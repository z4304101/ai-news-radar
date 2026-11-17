#!/usr/bin/env python3
"""Monitor source-backed 8-card GPU WHOLE-MACHINE monthly rental quotes.

IMPORTANT:
- No GPU-hour or single-card quotes are accepted.
- Never multiply an hourly rate by 8x720 to fabricate a monthly machine quote.
- Every reported number must be an explicitly published CNY/server/month value.
- Source dates and lease conditions are retained; missing models are unavailable.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

BEIJING = timezone(timedelta(hours=8))
SOURCES = (
    "https://chinasuanli.com/prices/?billing=monthly",
)
SPECS = (
    ("4090D", 24), ("4090", 24), ("5090", 32), ("L20", 48),
    ("910B2", 64), ("PRO6000D", 84), ("PRO6000", 96),
    ("A100", 80), ("A800", 80), ("H100", 80),
    ("H20", 96), ("H20", 141), ("H200", 141), ("B300", 288),
)


class VisibleText(HTMLParser):
    BREAKS = {"article", "section", "div", "p", "br", "h1", "h2", "h3",
              "li", "td", "th", "tr"}
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.ignored = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.ignored += 1
        elif not self.ignored and tag in self.BREAKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self.ignored = max(0, self.ignored - 1)
        elif not self.ignored and tag in self.BREAKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.ignored and data.strip():
            self.parts.append(data)

    def lines(self):
        text = "".join(self.parts)
        return [re.sub(r"\s+", " ", s).strip() for s in text.splitlines() if s.strip()]


def fetch_html(url):
    req = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; ai-news-radar/1.0; public-prices)",
        "Accept": "text/html,application/xhtml+xml",
    })
    with urlopen(req, timeout=25) as response:
        if response.status != 200:
            raise RuntimeError("Source returned HTTP " + str(response.status))
        raw = response.read(2_000_000)
        return raw.decode("utf-8", errors="replace")


def model_from_title(title):
    t = re.sub(r"\s+", "", title).upper()
    if "4090D" in t:
        name = "4090D"
    elif "4090" in t:
        name = "4090"
    elif "5090" in t:
        name = "5090"
    elif "L20" in t:
        name = "L20"
    elif "910B2" in t:
        name = "910B2"
    elif "910B" in t:
        return None  # Never label a generic 910B offer as specific 910B2.
    elif "PRO6000D" in t:
        name = "PRO6000D"
    elif "PRO6000" in t:
        name = "PRO6000"
    else:
        match = re.search(r"(H200|H100|H20|A100|A800|B300)", t)
        name = match.group(1) if match else None
    vram_match = re.search(r"(\d{2,3})\s*(?:GB|G)\b", title, re.I)
    vram = int(vram_match.group(1)) if vram_match else None
    return (name, vram) if (name, vram) in SPECS else None


def parse_dt(value):
    match = re.search(r"(\d{4})[./-](\d{2})[./-](\d{2})\s+(\d{2}):(\d{2})", value)
    if not match:
        return None
    return datetime(*(int(x) for x in match.groups()), tzinfo=BEIJING)


def parse_chinasuanli(html, now):
    parser = VisibleText()
    parser.feed(html)
    lines = parser.lines()
    rows = []
    monthly_indexes = [i for i, line in enumerate(lines) if "整机包月折算价" in line]
    for i in monthly_indexes:
        # Every monthly card must have its own published monthly fee and card count.
        stop = next((j for j in range(i + 1, min(i + 70, len(lines)))
                     if "采集：" in lines[j]), min(i + 70, len(lines)))
        block_lines = lines[i:stop + 1]
        block = " ".join(block_lines)

        title = next((s for s in block_lines[1:12]
                      if ("/" in s or "整机" in s or "服务器" in s)
                      and re.search(r"(RTX|4090|5090|A100|A800|H100|H20|H200|L20|B300|910B|PRO)", s, re.I)), "")
        model_spec = model_from_title(title)
        if not model_spec:
            continue
        if not (re.search(r"8\s*卡(?:整机|裸金属)", block, re.I) or
                re.search(r"单机卡数\s*8(?:\D|$)", block)):
            continue
        if not ("包月" in block or "元/月" in block):
            continue

        match = re.search(r"(?:官网整机挂牌价|整机活动价)\s*([\d,]+)\s*元\s*/\s*月", block)
        if not match:
            continue
        price = int(match.group(1).replace(",", ""))
        if not (500 <= price < 2_000_000):
            continue
        collected = re.search(r"采集[：:]\s*([\d./-]{10}\s+\d{2}:\d{2})", block)
        expiry = re.search(r"有效期至[：:]\s*([\d./-]{10}\s+\d{2}:\d{2})", block)
        as_of = parse_dt(collected.group(1)) if collected else None
        expires = parse_dt(expiry.group(1)) if expiry else None
        if not as_of or not expires or as_of > now or expires <= now:
            continue
        provider = block_lines[0].split("·")[0].strip()
        rows.append({
            "model": model_spec[0],
            "vram_gb": model_spec[1],
            "gpu_count": 8,
            "price_cny_server_month": price,
            "provider": provider,
            "pricing_type": "整机包月挂牌价（未经成交核验）",
            "rental_term": "页面标示包月；实际合同条款需核实",
            "collected_at": as_of.isoformat(),
            "expires_at": expires.isoformat(),
            "source_url": SOURCES[0],
            "original_title": title,
        })
    return rows, {"visible_lines": len(lines), "monthly_blocks": len(monthly_indexes)}


def summary(rows):
    result = []
    for name, vram in SPECS:
        matches = [r for r in rows if (r["model"], r["vram_gb"]) == (name, vram)]
        best = min(matches, key=lambda x: x["price_cny_server_month"]) if matches else None
        result.append({"model": name, "vram_gb": vram, "quote": best,
                       "valid_quote_count": len(matches)})
    return result


def report_table(items, now, source_errors):
    lines = [
        "# GPU 8 卡整机月租报价",
        "",
        "采集时间：" + now.strftime("%Y-%m-%d %H:%M 北京时间"),
        "统计口径：**人民币/台/月**，仅包含明确标注 8 卡整机、月租金额、有效期的公开挂牌报价。",
        "不包含云平台单卡/小时价；没有数据时不作乘法估值，也不把报价当作已成交价。",
        "",
        "| 8 卡整机配置 | 整机月租公开报价 | 发布方 | 采集/有效期 | 核对来源 |",
        "|---|---:|---|---|---|",
    ]
    for item in items:
        q = item["quote"]
        gpu = f'{item["model"]} {item["vram_gb"]}GB × 8'
        if q:
            lines.append(
                f'| {gpu} | ¥{q["price_cny_server_month"]:,}/台/月 '
                f'| {q["provider"]} | {q["collected_at"][:10]} / {q["expires_at"][:10]} '
                f'| [查看]({q["source_url"]}) |'
            )
        else:
            lines.append(f"| {gpu} | 暂无可核实整机月租 | - | - | - |")
    lines += [
        "", "缺失报价表示当前可访问数据源中无符合口径且未过期的样本。",
        "税费、押金、地域、配置和交付均以供应商合同为准。",
    ]
    if source_errors:
        lines += ["", "数据源检查异常：" + ", ".join(source_errors)]
    return "\n".join(lines) + "\n"


def push_bark(config, items, now):
    accounts = [x.strip() for x in config.split(";") if x.strip()][:3]
    if not accounts:
        raise RuntimeError("BARK_URL is missing; no notifications sent")
    found = [item for item in items if item["quote"]]
    if not found:
        print("No verified whole-machine prices: skip Bark notification.")
        return
    lines = [
        f'GPU 8卡整机月租｜{now.strftime("%m-%d %H:%M")} 北京时间',
        "人民币/台/月，公开挂牌报价，非已成交价格。",
        "",
    ]
    for item in found:
        q = item["quote"]
        lines.append(
            f'{item["model"]} {item["vram_gb"]}GB×8：'
            f'¥{q["price_cny_server_month"]:,}/月'
            f'（{q["provider"]}；核验至{q["expires_at"][:10]}）'
        )
    missing = len(items) - len(found)
    lines += ["", f"其余 {missing} 个型号：暂无符合口径的有效报价。",
              "来源：中国算力网·整机包月记录（请核对原始报价）", SOURCES[0]]
    payload = {"title": "TrendRadar GPU 8卡整机月租",
               "markdown": "\n".join(lines),
               "group": "TrendRadar GPU整机月租",
               "sound": "default", "action": "none"}
    successes = 0
    for index, uri in enumerate(accounts, 1):
        try:
            parsed = urlparse(uri)
            parts = [part for part in parsed.path.split("/") if part]
            if parsed.scheme != "https" or not parsed.netloc or not parts:
                raise ValueError("Bark account URL invalid")
            body = dict(payload, device_key=parts[0])
            req = Request(
                f"{parsed.scheme}://{parsed.netloc}/push",
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json; charset=utf-8"},
                method="POST",
            )
            with urlopen(req, timeout=30) as resp:
                if resp.status != 200:
                    raise RuntimeError("Bark HTTP status not 200")
                ack = json.load(resp)
                if not isinstance(ack, dict) or ack.get("code") != 200:
                    raise RuntimeError("Bark acknowledgement not 200")
            successes += 1
            print(f"Bark account {index}: accepted code=200")
        except Exception as exc:
            print(f"Bark account {index}: failed ({type(exc).__name__})", file=sys.stderr)
    if not successes:
        raise RuntimeError("All Bark accounts failed")
    print(f"Bark delivery accepted by {successes}/{len(accounts)} accounts.")


def main():
    now = datetime.now(BEIJING)
    rows = []
    errors = []
    for url in SOURCES:
        try:
            html = fetch_html(url)
            parsed, stats = parse_chinasuanli(html, now)
            rows.extend(parsed)
            print(f"Source checked, {len(parsed)} exact 8-card monthly records, parsing stats: {stats}")
        except Exception as exc:
            errors.append(type(exc).__name__)
            print(f"Failed to read whole-machine quote source ({type(exc).__name__}).", file=sys.stderr)
    items = summary(rows)
    print("Verified model coverage:", sum(bool(x["quote"]) for x in items), "/", len(items))
    for item in items:
        q = item["quote"]
        print(f'  {item["model"]} {item["vram_gb"]}GB: ' +
              (f'CNY {q["price_cny_server_month"]}/server/month' if q else "no verified quote"))

    if not rows:
        print("No validated 8-card monthly prices; no push.", file=sys.stderr)
        return 2

    output = Path(os.environ.get("GPU_WHOLE_OUTPUT", "output/gpu_whole_prices"))
    output.mkdir(parents=True, exist_ok=True)
    data = {"generated_at": now.isoformat(), "unit": "CNY/server/month",
            "gpu_count": 8, "price_kind": "public_asking_quote_not_trade",
            "quotes": rows, "coverage": len([x for x in items if x["quote"]]),
            "missing_models": [f'{i["model"]} {i["vram_gb"]}GB' for i in items if not i["quote"]],
            "source_errors": errors}
    (output / "latest.md").write_text(report_table(items, now, errors), encoding="utf-8")
    prior_hash = (output / "last_notified.sha256").read_text().strip() if (
        output / "last_notified.sha256").exists() else ""
    fingerprint = hashlib.sha256(json.dumps(
        sorted((x["model"], x["vram_gb"], x["price_cny_server_month"],
                x["provider"], x["expires_at"]) for x in rows),
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    (output / "latest.json").write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as f:
            f.write(report_table(items, now, errors))
    notify = os.getenv("GPU_WHOLE_NOTIFY", "false").lower() == "true"
    if notify and prior_hash != fingerprint:
        push_bark(os.environ.get("BARK_URL", ""), items, now)
        (output / "last_notified.sha256").write_text(fingerprint + "\n")
    elif notify:
        print("Whole-machine quote set unchanged: no repeated Bark push.")
    else:
        print("Whole-machine Bark push disabled for smoke test.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
