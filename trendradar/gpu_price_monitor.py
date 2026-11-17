#!/usr/bin/env python3
"""Standalone public GPU rental quote tracker for ai-news-radar.

Source: https://gpuprice.cn/developers (attribution required).
All prices are PUBLIC OFFERS, not confirmed completed transactions.
The source normalizes priceCny to CNY / GPU-hour; do not infer 8-card
whole-machine prices from per-GPU quotes.
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

API = "https://gpuprice.cn/api"
SOURCE = "https://gpuprice.cn"
MODELS = (
    ("PRO6000D", r"(?<![A-Z0-9])(?:RTX[\s-]*)?PRO[\s-]*6000[\s-]*D(?![A-Z0-9])"),
    ("PRO6000", r"(?<![A-Z0-9])(?:RTX[\s-]*)?PRO[\s-]*6000(?![\s-]*D)(?![A-Z0-9])"),
    ("4090D", r"(?<![A-Z0-9])(?:RTX[\s-]*)?4090[\s-]*D(?![A-Z0-9])"),
    ("4090", r"(?<![A-Z0-9])(?:RTX[\s-]*)?4090(?![\s-]*D)(?![A-Z0-9])"),
    ("5090", r"(?<![A-Z0-9])(?:RTX[\s-]*)?5090(?![A-Z0-9])"),
    ("910B2", r"(?<![A-Z0-9])(?:ASCEND[\s-]*)?910[\s-]*B2(?![A-Z0-9])"),
    ("H200", r"(?<![A-Z0-9])H200(?![A-Z0-9])"),
    ("H100", r"(?<![A-Z0-9])H100(?![A-Z0-9])"),
    ("H20", r"(?<![A-Z0-9])H20(?![A-Z0-9])"),
    ("A100", r"(?<![A-Z0-9])A100(?![A-Z0-9])"),
    ("A800", r"(?<![A-Z0-9])A800(?![A-Z0-9])"),
    ("L20", r"(?<![A-Z0-9])L20(?![A-Z0-9])"),
    ("B300", r"(?<![A-Z0-9])B300(?![A-Z0-9])"),
)
LABELS = [name for name, _ in MODELS]
# Keep memory variants distinct; do not label an A100-40G quote as A100-80G,
# or an H100-NVL-94G quote as standard H100-80G.
DISPLAY_SPECS = (
    ("4090D 24GB", "4090D", 24),
    ("4090 24GB", "4090", 24),
    ("5090 32GB", "5090", 32),
    ("L20 48GB", "L20", 48),
    ("910B2 64GB", "910B2", 64),
    ("PRO6000D 84GB", "PRO6000D", 84),
    ("PRO6000 96GB", "PRO6000", 96),
    ("A100 80GB", "A100", 80),
    ("A800 80GB", "A800", 80),
    ("H100 80GB", "H100", 80),
    ("H20 96GB", "H20", 96),
    ("H20 141GB", "H20", 141),
    ("H200 141GB", "H200", 141),
    ("B300 288GB", "B300", 288),
)
PATTERNS = [(name, re.compile(pattern, re.I)) for name, pattern in MODELS]
SLUGS = {
    "4090": "rtx-4090", "4090D": "rtx-4090d", "5090": "rtx-5090",
    "L20": "l20", "910B2": "ascend-910b2",
    "PRO6000": "rtx-pro-6000", "PRO6000D": "rtx-pro-6000d",
    "A100": "a100-80g", "A800": "a800-80g", "H100": "h100",
    "H20": "h20-96g", "H200": "h200", "B300": "b300",
}


def request_json(url: str):
    req = Request(url, headers={"User-Agent": "ai-news-radar-GPU-Price-Monitor/1.0",
                                "Accept": "application/json"})
    with urlopen(req, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError("price source returned a non-200 HTTP status")
        return json.load(response)


def entries(data, expected_key):
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if not isinstance(data, dict):
        return []
    for name in (expected_key, "data", "items", "results", "list"):
        found = data.get(name)
        if isinstance(found, list):
            return [row for row in found if isinstance(row, dict)]
        if isinstance(found, dict):
            inner = entries(found, expected_key)
            if inner:
                return inner
    # Accept a map keyed by id or slug.
    if data and all(isinstance(v, dict) for v in data.values()):
        return [dict(v, _map_key=k) for k, v in data.items()]
    return []


def pick(obj, *keys):
    if not isinstance(obj, dict):
        return None
    for key in keys:
        if key in obj and obj[key] is not None and obj[key] != "":
            return obj[key]
    return None


def ident(obj):
    if isinstance(obj, dict):
        return pick(obj, "id", "slug", "gpuId", "gpu_id", "name", "key")
    return obj


def title(obj):
    if isinstance(obj, dict):
        return str(pick(obj, "name", "gpuName", "modelName", "model",
                        "title", "slug", "id") or "")
    return str(obj or "")


def model_for(text):
    for name, pattern in PATTERNS:
        if pattern.search(text):
            return name
    return None


def index_rows(rows):
    indexed = {}
    for row in rows:
        for key in ("id", "slug", "key", "_map_key"):
            value = row.get(key)
            if value is not None:
                indexed[str(value)] = row
    return indexed


def observed(value):
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000 if value > 1e11 else value,
                                          tz=timezone.utc)
        except (ValueError, OverflowError):
            return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(
            tzinfo=timezone.utc
        ) if datetime.fromisoformat(str(value).replace("Z", "+00:00")).tzinfo is None else (
            datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
        )
    except (TypeError, ValueError):
        return None


def numeric(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(str(value).replace(",", "").replace("¥", ""))
    except ValueError:
        return None
    return num if 0 < num < 1000000 else None


def resolve_offer(offer, gpus, providers, now):
    gpu_value = pick(offer, "gpu", "gpuName", "gpu_name", "gpuModel", "gpu_model",
                     "gpuSlug", "gpu_slug", "model", "modelName", "sku",
                     "gpuId", "gpu_id", "gpu_id", "product")
    gpu_info = gpus.get(str(ident(gpu_value)), {})
    if not gpu_info and isinstance(gpu_value, dict):
        gpu_info = gpu_value
    names = " ".join(filter(None, (
        title(gpu_value), title(gpu_info), title(pick(offer, "name", "title")),
        str(pick(offer, "gpuSlug", "gpuId", "gpu_id", "model") or ""),
    )))
    model = model_for(names)
    if model is None:
        return None

    normalized = numeric(pick(offer, "priceCny", "price_cny"))
    if normalized is None:
        return None

    timestamp_raw = pick(offer, "observedAt", "observed_at", "checkedAt",
                         "checked_at", "updatedAt", "updated_at")
    seen = observed(timestamp_raw)
    if seen is None or seen < now - timedelta(days=7) or seen > now + timedelta(hours=2):
        return None

    stock = str(pick(offer, "stock", "availability") or "unknown").lower()
    if stock in ("soldout", "sold_out", "unavailable", "out_of_stock"):
        return None

    billing = str(pick(offer, "billing", "billingType", "billing_type") or "unknown").lower()
    if billing not in ("ondemand", "monthly", "spot"):
        return None

    raw_provider = pick(offer, "provider", "providerName", "provider_name",
                        "vendor", "providerId", "provider_id", "platform", "platformId")
    provider = providers.get(str(ident(raw_provider)), {})
    vendor = title(raw_provider) if isinstance(raw_provider, dict) else (
        title(provider) or str(raw_provider or "来源未标注"))
    if vendor.isdigit() and provider:
        vendor = title(provider)
    if not vendor:
        vendor = "来源未标注"

    # Keep regional classification conservative: we do not deduce location
    # from currency because foreign prices are converted to CNY.
    raw_region = str(pick(offer, "market", "region", "country", "countryCode")
                     or pick(provider, "market", "region", "country", "countryCode") or "").lower()
    if raw_region in ("cn", "china", "domestic", "中国", "中国大陆"):
        region = "国内"
    elif raw_region in ("overseas", "global", "international", "foreign"):
        region = "海外"
    elif raw_region:
        region = "海外" if raw_region not in ("unknown", "na") else "未标注"
    else:
        region = "未标注"

    vram = pick(offer, "vramGb", "vramGB", "vram", "memoryGb", "memory_gb")
    if vram is None:
        vram = pick(gpu_info, "vramGb", "vramGB", "vram", "memoryGb", "memory_gb")
    if vram is None:
        match = re.search(r"\b(\d{1,3})\s*(?:GB|G)\b", names, re.I)
        vram = match.group(1) if match else ""

    offer_url = str(pick(offer, "url", "offerUrl", "offer_url", "sourceUrl",
                         "source_url", "link") or "")
    if not offer_url.startswith("https://"):
        offer_url = SOURCE + "/gpu/" + SLUGS[model]
    return {
        "model": model, "vram_gb": str(vram), "provider": vendor,
        "price_cny_gpu_hour": normalized, "billing": billing,
        "stock": stock, "region": region, "observed_at": seen.isoformat(),
        "source_url": offer_url, "data_source": SOURCE,
        "price_kind": "公开市场报价（未验证成交）",
        "gpu_count": pick(offer, "gpuCount", "gpu_count", "cardCount", "card_count") or "",
    }


def summarize(rows):
    result = []
    for label, model, vram in DISPLAY_SPECS:
        offers = [
            x for x in rows
            if x["model"] == model and x["billing"] == "ondemand"
            and str(x["vram_gb"]).strip().upper().replace("GB", "").replace("G", "").strip() == str(vram)
        ]
        best = min(offers, key=lambda r: r["price_cny_gpu_hour"]) if offers else None
        result.append({"model": label, "quote": best, "offer_count": len(offers)})
    return result


def make_report(summary, now):
    lines = [
        "# GPU 租赁公开报价日报",
        "",
        "更新：" + now.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M 北京时间"),
        "",
        "数据来源：GPU价网 https://gpuprice.cn （公开挂牌/租赁报价，并非真实成交数据）",
        "口径：每张 GPU 每小时人民币价格；仅使用近 7 天有采集时间、未售罄的按量报价。",
        "注意：包月、抢占式报价和整机价不混入按量最低价；不推算 8 卡整机合同价。",
        "",
        "| GPU 型号 | 最低公开按量报价（人民币/GPU·小时） | 平台 | 报价时间 | 来源 |",
        "|---|---:|---|---|---|",
    ]
    for item in summary:
        best = item["quote"]
        if best:
            lines.append("| {model} | ¥{price:.2f} | {vendor} | {seen} | [详情]({url}) |".format(
                model=item["model"], price=best["price_cny_gpu_hour"],
                vendor=best["provider"].replace("|", "/"),
                seen=best["observed_at"][:16], url=best["source_url"]))
        else:
            lines.append(f"| {item['model']} | 暂无可验证报价 | - | - | - |")
    lines += ["", "未展示价格不代表无法租赁，只表示该数据源暂无符合口径的报价。"]
    return "\n".join(lines) + "\n"


def bark_push(bark_url, summary):
    if not bark_url:
        print("BARK_URL not provided; skipping GPU price notification.")
        return
    lines = ["公开按量报价，元/卡·小时，非真实成交价。"]
    for item in summary:
        best = item["quote"]
        if best:
            lines.append(f"{item['model']}: ¥{best['price_cny_gpu_hour']:.2f} ({best['provider']})")
        else:
            lines.append(f"{item['model']}: 暂无有效报价")
    lines.extend(["来源：GPU价网；时间、计费条件以报价页为准。", SOURCE])
    body = "\n".join(lines)
    payload = {"title": "GPU 租赁价格日报", "body": body,
               "group": "GPU算力价格", "url": SOURCE}
    endpoint = bark_url.strip()
    if not endpoint.startswith("https://"):
        raise ValueError("BARK_URL must use HTTPS")
    req = Request(endpoint, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                  headers={"Content-Type": "application/json; charset=utf-8"},
                  method="POST")
    # Do not print the URL or response body because it may contain a device key.
    with urlopen(req, timeout=20) as response:
        if response.status != 200:
            raise RuntimeError("Bark server returned a non-200 status")
        raw = json.load(response)
        if isinstance(raw, dict) and raw.get("code") not in (None, 200):
            raise RuntimeError("Bark server did not confirm a successful push")
    print("GPU price notification accepted by Bark server.")


def main():
    now = datetime.now(timezone.utc)
    offers_data = request_json(API + "/offers.json")
    gpus_data = request_json(API + "/gpus.json")
    providers_data = request_json(API + "/providers.json")
    raw_offers = entries(offers_data, "offers")
    print(f"Loaded {len(raw_offers)} public offers; no credentials required.")
    if not raw_offers:
        print("Provider JSON structure not recognized, no price notification sent.", file=sys.stderr)
        return 2
    gpus = index_rows(entries(gpus_data, "gpus"))
    providers = index_rows(entries(providers_data, "providers"))
    resolved = [row for offer in raw_offers
                if (row := resolve_offer(offer, gpus, providers, now)) is not None]
    summary = summarize(resolved)
    count = sum(bool(item["quote"]) for item in summary)
    print(f"Matched {len(resolved)} eligible public quotes, covering {count}/{len(DISPLAY_SPECS)} GPU variants.")
    for item in summary:
        offer = item["quote"]
        print(f"  {item['model']}: " + (f"CNY {offer['price_cny_gpu_hour']:.2f}/GPU-hour, {offer['provider']}" if offer else "no current on-demand quote"))
    if not resolved or count == 0:
        # Do not report a false price table if API schema changes.
        print("No verified price records; leaving existing TrendRadar untouched.", file=sys.stderr)
        print("Offer fields: " + ", ".join(sorted(raw_offers[0].keys())), file=sys.stderr)
        return 3

    output = Path(os.environ.get("GPU_PRICE_OUTPUT", "output/gpu_prices"))
    output.mkdir(parents=True, exist_ok=True)
    report = make_report(summary, now)
    (output / "latest.md").write_text(report, encoding="utf-8")
    (output / "latest.json").write_text(json.dumps({
        "generated_at": now.isoformat(),
        "source": SOURCE, "quote_type": "public_offer_not_confirmed_trade",
        "offers": resolved, "summary": summary,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    with (output / "offers.csv").open("w", newline="", encoding="utf-8-sig") as f:
        fields = ("model", "vram_gb", "provider", "price_cny_gpu_hour",
                  "billing", "stock", "region", "observed_at",
                  "source_url", "data_source", "price_kind", "gpu_count")
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(resolved)
    summary_file = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as f:
            f.write(report)

    if os.getenv("GPU_PRICE_NOTIFY", "false").lower() == "true":
        bark_push(os.getenv("BARK_URL", ""), summary)
    else:
        print("Push disabled for this test run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
