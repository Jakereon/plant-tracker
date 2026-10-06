"""Identify a plant from a photo: Pl@ntNet first, local Ollama vision model as fallback."""
import base64
import json
import logging

import httpx

from . import db
from .research import UA, Skip

log = logging.getLogger("identify")
LOW_CONFIDENCE = 0.20


async def plantnet(client, jpeg: bytes, cfg) -> list[dict]:
    key = cfg("plantnet_key", "PLANTNET_API_KEY")
    if not key:
        raise Skip("no API key")
    r = await client.post(
        "https://my-api.plantnet.org/v2/identify/all",
        params={"api-key": key, "include-related-images": "true", "nb-results": 6, "lang": "en"},
        files=[("images", ("photo.jpg", jpeg, "image/jpeg"))],
        data={"organs": "auto"},
    )
    if r.status_code == 404:  # "Species not found"
        return []
    r.raise_for_status()
    out = []
    for res in r.json().get("results") or []:
        sp = res.get("species") or {}
        out.append({
            "scientific_name": sp.get("scientificNameWithoutAuthor") or sp.get("scientificName"),
            "common_name": ", ".join((sp.get("commonNames") or [])[:3]),
            "family": (sp.get("family") or {}).get("scientificNameWithoutAuthor", ""),
            "score": float(res.get("score") or 0),
            "images": [((i.get("url") or {}).get("m") or (i.get("url") or {}).get("s"))
                       for i in (res.get("images") or [])[:4]],
            "reason": "",
            "source": "Pl@ntNet",
        })
    return [c for c in out if c["scientific_name"]]


async def ollama_vision(client, jpeg: bytes, cfg) -> tuple[list[dict], str]:
    url = cfg("ollama_url", "OLLAMA_URL")
    if not url:
        raise Skip("OLLAMA_URL not set")
    model = cfg("ollama_vision_model", "OLLAMA_VISION_MODEL") or "qwen2.5vl:7b"
    schema = {
        "type": "object",
        "properties": {"candidates": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "scientific_name": {"type": "string"},
                "common_name": {"type": "string"},
                "confidence": {"type": "number"},
                "reason": {"type": "string"},
            },
            "required": ["scientific_name", "common_name", "confidence", "reason"],
        }}},
        "required": ["candidates"],
    }
    prompt = ("Identify the plant in this photo. Give up to 3 candidate species, most likely first, "
              "with a confidence between 0 and 1 and the visible features that support each guess. "
              "If it is a common houseplant cultivar, name the species.")
    r = await client.post(url.rstrip("/") + "/api/chat", timeout=600, json={
        "model": model, "stream": False, "format": schema, "options": {"temperature": 0.1},
        "messages": [{"role": "user", "content": prompt, "images": [base64.b64encode(jpeg).decode()]}],
    })
    r.raise_for_status()
    data = json.loads(r.json()["message"]["content"])
    out = []
    for c in data.get("candidates") or []:
        if not c.get("scientific_name"):
            continue
        try:
            score = max(0.0, min(1.0, float(c.get("confidence") or 0)))
        except (TypeError, ValueError):
            score = 0.0
        out.append({"scientific_name": c["scientific_name"], "common_name": c.get("common_name", ""),
                    "family": "", "score": score, "images": [], "reason": c.get("reason", ""),
                    "source": f"Ollama ({model})"})
    return out[:3], model


async def identify(jpeg: bytes) -> dict:
    settings = db.get_settings()
    cfg = lambda k, env: db.cfg(settings, k, env)  # noqa: E731
    candidates, logs = [], []
    async with httpx.AsyncClient(timeout=60, headers=UA) as client:
        try:
            found = await plantnet(client, jpeg, cfg)
            candidates += found
            logs.append({"provider": "Pl@ntNet", "status": "ok" if found else "no match",
                         "detail": f"top match {found[0]['score']:.0%}" if found else ""})
        except Skip as e:
            logs.append({"provider": "Pl@ntNet", "status": "skipped", "detail": str(e)})
        except Exception as e:
            log.warning("Pl@ntNet failed: %s", e)
            logs.append({"provider": "Pl@ntNet", "status": "error", "detail": f"{type(e).__name__}: {e}"[:240]})

        if not candidates or candidates[0]["score"] < LOW_CONFIDENCE:
            try:
                found, model = await ollama_vision(client, jpeg, cfg)
                candidates += found
                logs.append({"provider": f"Ollama ({model})", "status": "ok" if found else "no match", "detail": ""})
            except Skip as e:
                logs.append({"provider": "Ollama vision", "status": "skipped", "detail": str(e)})
            except Exception as e:
                log.warning("Ollama vision failed: %s", e)
                logs.append({"provider": "Ollama vision", "status": "error",
                             "detail": f"{type(e).__name__}: {e}"[:240]})
        else:
            logs.append({"provider": "Ollama vision", "status": "not needed",
                         "detail": "Pl@ntNet was confident enough"})
    return {"candidates": candidates, "log": logs}
