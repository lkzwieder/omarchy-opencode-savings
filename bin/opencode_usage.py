"""Read opencode's session database and price its tokens against the cloud.

Nothing here is specific to one machine's model line-up. A model counts as
free when opencode itself says it costs nothing, which covers local runtimes
(llama.cpp, Ollama, vLLM behind LiteLLM) and the zero-cost models on hosted
gateways. To say what that traffic *would* have cost, each model is matched by
display name against the models.dev catalog opencode already caches, and the
median published price of the matches is used.
"""
import collections
import datetime
import json
import os
import re
import statistics

DB = os.path.expanduser("~/.local/share/opencode/opencode.db")
CATALOG = os.path.expanduser("~/.cache/opencode/models.json")
CONFIG_DIRS = ["~/.config/opencode", "~/.opencode"]
OVERRIDES = os.path.expanduser("~/.config/omarchy/agents/opencode.json")

# in, out, cache_read, cache_write, in USD per 1M tokens.
FALLBACK_PRICE = (0.20, 1.00, 0.05, 0.05)
# The default reference is a price point, not a product: frontier-tier rates as
# published across hosted providers. Name an actual model in the overrides file
# if you want the comparison quoted against one.
DEFAULT_BASELINE = ("frontier tier", (5.00, 25.00, 0.50, 6.25))
STOPWORDS = {"free", "thinking", "instruct", "chat", "preview", "beta", "tee",
             "fp8", "bf16", "nvfp4", "awq", "gguf", "uncensored", "heretic",
             "obliterated", "abliterated"}


# --------------------------------------------------------------- user config

def overrides():
    """Optional ~/.config/omarchy/agents/opencode.json.

    {"baseline": {"name": "...", "price": [in, out, cacheRead, cacheWrite]},
     "equivalents": {"provider/model": ["catalogProvider", "catalogModel"]},
     "free": ["provider/model"], "paid": ["provider/model"]}
    """
    try:
        with open(OVERRIDES) as fh:
            return json.load(fh)
    except Exception:
        return {}


def baseline():
    cfg = overrides().get("baseline") or {}
    if cfg.get("price"):
        return str(cfg.get("name", "baseline")), tuple(cfg["price"])
    return DEFAULT_BASELINE


def catalog():
    try:
        with open(CATALOG) as fh:
            return json.load(fh)
    except Exception:
        return {}


def opencode_config():
    """The user's own provider/model declarations, merged across config files."""
    merged = {}
    for directory in CONFIG_DIRS:
        path = os.path.join(os.path.expanduser(directory), "opencode.json")
        try:
            with open(path) as fh:
                data = json.load(fh)
        except Exception:
            continue
        for provider, spec in (data.get("provider") or {}).items():
            entry = merged.setdefault(provider, {"name": "", "models": {}})
            entry["name"] = spec.get("name") or entry["name"]
            entry["models"].update(spec.get("models") or {})
    return merged


# ---------------------------------------------------------------- pricing

def _price_tuple(cost):
    if not cost:
        return None
    inp, out = cost.get("input"), cost.get("output")
    if not inp and not out:
        return None
    inp = inp or 0.0
    return (inp, out or 0.0,
            cost.get("cache_read", inp * 0.1),
            cost.get("cache_write", inp * 1.25))


def catalog_price(cat, provider, model):
    entry = ((cat.get(provider) or {}).get("models") or {}).get(model) or {}
    return _price_tuple(entry.get("cost"))


def _normalize(text):
    text = re.sub(r"\(.*?\)", " ", str(text or "")).lower()
    words = [w for w in re.split(r"[^a-z0-9.]+", text) if w and w not in STOPWORDS]
    return words


def _match_score(wanted, candidate):
    """How many leading words of the wanted name the candidate reproduces."""
    score = 0
    for word in wanted:
        if word in candidate:
            score += 1
        elif score:
            break
    return score


def equivalent_price(cat, display_name, fallback=FALLBACK_PRICE):
    """Median published price of the catalog models whose name matches."""
    wanted = _normalize(display_name)
    if len(wanted) < 2:
        return fallback
    best, matches = 0, []
    for provider, spec in cat.items():
        for model_id, model in (spec.get("models") or {}).items():
            price = _price_tuple(model.get("cost"))
            if not price:
                continue
            score = max(_match_score(wanted, _normalize(model.get("name"))),
                        _match_score(wanted, _normalize(model_id)))
            if score < 2 or score < best:
                continue
            if score > best:
                best, matches = score, []
            matches.append(price)
    if not matches:
        return fallback
    return tuple(statistics.median(m[i] for m in matches) for i in range(4))


class Pricer:
    """Decides, per model, what it cost and what it would have cost."""

    def __init__(self):
        self.catalog = catalog()
        self.config = opencode_config()
        cfg = overrides()
        self.explicit = {k: tuple(v) for k, v in (cfg.get("equivalents") or {}).items()}
        self.forced_free = set(cfg.get("free") or ())
        self.forced_paid = set(cfg.get("paid") or ())
        self._cache = {}

    def _declared(self, provider, model):
        return ((self.config.get(provider) or {}).get("models") or {}).get(model)

    def display_name(self, provider, model):
        declared = self._declared(provider, model)
        if declared and declared.get("name"):
            return declared["name"]
        entry = ((self.catalog.get(provider) or {}).get("models") or {}).get(model) or {}
        return entry.get("name") or model

    def short_name(self, provider, model):
        """The display name without the runtime detail a panel has no room for."""
        name = re.sub(r"\s*\(.*?\)", "", self.display_name(provider, model)).strip()
        return name or model

    def is_free(self, key):
        if key in self.forced_paid:
            return False
        if key in self.forced_free:
            return True
        provider, _, model = key.partition("/")
        declared = self._declared(provider, model)
        if declared is not None:
            cost = declared.get("cost") or {}
            return not (cost.get("input") or cost.get("output"))
        return catalog_price(self.catalog, provider, model) is None

    def price(self, key):
        """(actually charged, cloud equivalent) for one model."""
        if key in self._cache:
            return self._cache[key]
        provider, _, model = key.partition("/")
        if key in self.explicit:
            equivalent = catalog_price(self.catalog, *self.explicit[key]) or FALLBACK_PRICE
        else:
            equivalent = (catalog_price(self.catalog, provider, model)
                          or equivalent_price(self.catalog,
                                              self.display_name(provider, model)))
        charged = None if self.is_free(key) else equivalent
        self._cache[key] = (charged, equivalent)
        return self._cache[key]


def cost(counters, price):
    if not price:
        return 0.0
    return (counters.get("input", 0) * price[0]
            + counters.get("output", 0) * price[1]
            + counters.get("cache_read", 0) * price[2]
            + counters.get("cache_write", 0) * price[3]) / 1e6


# ------------------------------------------------------------------ reading

def collect(days=3650):
    """-> (byModel, byDay, byDayAndModel, sessionsPerDay, totalSessions)"""
    empty = ({}, {}, {}, {}, 0)
    if not os.path.exists(DB):
        return empty
    import sqlite3
    con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).timestamp() * 1000
    by_model = collections.defaultdict(collections.Counter)
    by_day = collections.defaultdict(collections.Counter)
    by_day_model = collections.defaultdict(lambda: collections.defaultdict(collections.Counter))
    sessions = collections.defaultdict(set)
    all_sessions = set()
    try:
        rows = con.execute(
            "select time_created, data, session_id from message where time_created >= ?",
            (cutoff,))
    except Exception:
        return empty
    for created, data, session_id in rows:
        try:
            message = json.loads(data)
        except Exception:
            continue
        day = datetime.datetime.fromtimestamp(created / 1000).date()
        if message.get("role") != "assistant":
            if message.get("role") == "user":
                by_day[day]["prompts"] += 1
            continue
        tokens = message.get("tokens") or {}
        cache = tokens.get("cache") or {}
        key = "%s/%s" % (message.get("providerID"), message.get("modelID"))
        values = {"input": tokens.get("input", 0),
                  "output": tokens.get("output", 0) + tokens.get("reasoning", 0),
                  "cache_read": cache.get("read", 0),
                  "cache_write": cache.get("write", 0),
                  "msgs": 1}
        values["total"] = sum(values[k] for k in
                              ("input", "output", "cache_read", "cache_write"))
        for name, value in values.items():
            by_model[key][name] += value
            by_day[day][name] += value
            by_day_model[day][key][name] += value
        sessions[day].add(session_id)
        all_sessions.add(session_id)
    return dict(by_model), dict(by_day), by_day_model, sessions, len(all_sessions)


def rate(per_model, pricer=None, base_price=None):
    """-> (paid, sameModelInCloud, baselineModel) in USD."""
    pricer = pricer or Pricer()
    base_price = base_price or baseline()[1]
    paid = equivalent = 0.0
    totals = collections.Counter()
    for key, counters in per_model.items():
        charged, cloud = pricer.price(key)
        paid += cost(counters, charged)
        equivalent += cost(counters, cloud)
        totals.update(counters)
    return paid, equivalent, cost(totals, base_price)
