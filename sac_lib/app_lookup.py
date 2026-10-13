"""Resolve exact names without the retired Steam GetAppList endpoint."""

import json
import unicodedata

import requests


def normalized_name(value):
    return unicodedata.normalize("NFKC", value).strip().casefold()


def exact_app_id(items, name, id_key):
    matches = {
        item[id_key]
        for item in items
        if isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and normalized_name(item["name"]) == normalized_name(name)
        and type(item.get(id_key)) is int
        and item[id_key] > 0
        and item.get("type", "app") == "app"
    }
    if len(matches) > 1:
        raise ValueError("Multiple exact matches found; enter the AppID instead.")
    return next(iter(matches), 0)


def resolve_app_name(name, cache_path="applist.txt", get=None):
    if not name.strip():
        return 0
    try:
        with open(cache_path, encoding="utf-8") as file:
            data = json.load(file)
        items = data["applist"]["apps"]
        if not isinstance(items, list):
            raise ValueError("Invalid cached app list")
    except (OSError, ValueError, KeyError, TypeError):
        items = []
    cached_id = exact_app_id(items, name, "appid")
    if cached_id:
        return cached_id

    response = (get or requests.get)(
        "https://store.steampowered.com/api/storesearch/",
        params={"term": name.strip(), "l": "english", "cc": "US"},
        timeout=10,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("Invalid Steam search response")
    return exact_app_id(data["items"], name, "id")
