#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate a one-line-per-stock concise briefing from a full daily report.

Reads ``reports/report_YYYYMMDD.md`` (and, when present, the sibling
``market_review_YYYYMMDD.md`` and ``logs/stock_analysis_YYYYMMDD.log``) and
writes ``reports/report_YYYYMMDD_simple.md`` next to it.

Only the stable, template-generated structures of the full report are
parsed: the per-stock "核心结论" block (action + 一句话决策), the
"当日行情" table (close + change %), and the "操作点位" table (ideal
entry / stop-loss / target). The full report itself is never modified.
No network access and no LLM calls are made.

Usage:
    python scripts/make_concise_report.py [report ...]
    python scripts/make_concise_report.py --reports-dir reports
"""

import argparse
import ast
import re
import sys
from pathlib import Path

# Actions as printed in the full report, in overview display order.
ACTION_ORDER = ["买入", "持有", "观望", "减仓", "卖出", "加仓", "清仓"]
ACTION_PATTERN = "|".join(ACTION_ORDER)

# Corporate suffixes stripped from stock names for the one-line display
# (e.g. "腾讯控股" -> "腾讯", "阿里巴巴-W" -> "阿里巴巴").
NAME_SUFFIXES = ("控股", "集团", "时代", "股份", "有限公司", "-W", "-SW")

# Section headings used by the report template.
RE_STOCK_HEADING = re.compile(
    r"^##\s+\S+\s+(.+?)\s*\(([A-Za-z0-9.\-]+)\)\s*$"
)
RE_TITLE_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
RE_FILENAME_DATE = re.compile(r"report_(\d{8})\.md$")
RE_ACTION = re.compile(
    r"^\*\*[^*]*?(" + ACTION_PATTERN + r")\*\*\s*\|", re.M
)
RE_DECISION = re.compile(r"^>\s*\*\*一句话决策\*\*[:：]\s*(.+?)\s*$", re.M)
RE_MARKET_STATUS = re.compile(r"市场状态[:：]\s*(.+?)\s*$", re.M)
RE_NUMBER = re.compile(r"(\d+(?:\.\d+)?)")
RE_CONFIDENCE = re.compile(
    r"(?:置信度[^。；\n]{0,12}?(高|中|低)|(中等|偏低|偏高)置信度)"
)
RE_SKIPPED = re.compile(r"今日休市股票已跳过:\s*(\{[^}]*\})")


def warn(message):
    """Print a warning to stderr without failing the run."""
    print(f"warning: {message}", file=sys.stderr)


def shorten_name(name):
    """Strip generic corporate suffixes; never hard-code a stock."""
    short = name.strip()
    changed = True
    while changed:
        changed = False
        for suffix in NAME_SUFFIXES:
            if short.endswith(suffix) and len(short) > len(suffix):
                short = short[: -len(suffix)]
                changed = True
    return short


def fmt_num(value):
    """Format a price with at most 2 decimals, trailing zeros stripped."""
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text


def first_number(cell):
    """Return the first number in a table cell as a formatted string."""
    match = RE_NUMBER.search(cell or "")
    if not match:
        return None
    return fmt_num(float(match.group(1)))


def split_table_row(line):
    """Split a Markdown table row into stripped cells."""
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def parse_stock_section(lines):
    """Parse one ``## <emoji> <name> (<code>)`` section.

    Returns a dict with name/code/action/decision/close/change/points, or
    None when the section lacks a recognisable action line.
    """
    text = "\n".join(lines)
    stock = {"gaps": set(), "confidence": None}

    action_match = RE_ACTION.search(text)
    if not action_match:
        return None
    stock["action"] = action_match.group(1)

    decision_match = RE_DECISION.search(text)
    if decision_match:
        stock["decision"] = decision_match.group(1).strip()
    else:
        # Fall back to the dominant strongest signal.
        bullish = re.search(r"最强看多信号\*\*[:：]\s*(.+?)\s*$", text, re.M)
        bearish = re.search(r"最强看空信号\*\*[:：]\s*(.+?)\s*$", text, re.M)
        if stock["action"] in ("买入", "加仓", "持有") and bullish:
            stock["decision"] = bullish.group(1).strip()
        elif bearish:
            stock["decision"] = bearish.group(1).strip()
        else:
            stock["decision"] = ""

    # "当日行情" table: header row starts with 收盘, data row follows the
    # separator line. Close is column 1, change % is column 6.
    stock["close"] = None
    stock["change"] = None
    for i, line in enumerate(lines):
        if line.startswith("| 收盘 |"):
            if i + 2 < len(lines):
                cells = split_table_row(lines[i + 2])
                if len(cells) >= 6:
                    stock["close"] = cells[0] or None
                    change = cells[5]
                    if re.match(r"^[+-]?\d+(?:\.\d+)?%$", change):
                        stock["change"] = (
                            change
                            if change.startswith(("+", "-"))
                            else f"+{change}"
                        )
            break

    # "操作点位" table: ideal entry / stop-loss / target cells.
    points = {"buy": None, "buy_deferred": False, "stop": None, "target": None}
    for line in lines:
        cells = split_table_row(line)
        if len(cells) < 2:
            continue
        label, cell = cells[0], cells[1]
        if "理想买入点" in label:
            points["buy"] = first_number(cell)
            points["buy_deferred"] = "暂不" in cell
        elif "止损位" in label:
            points["stop"] = first_number(cell)
        elif "目标位" in label:
            points["target"] = first_number(cell)
    stock["points"] = points

    # Data gaps: look for "<type> ... 缺失/N/A" within a short window, so
    # phrasings like "财报结构化字段（...）均为N/A" are still caught.
    if re.search(r"新闻[^。\n]{0,20}?(缺失|missing|未检索到|检索为空)", text):
        stock["gaps"].add("新闻")
    if re.search(r"筹码[^。\n]{0,20}?(缺失|missing|未启用|暂不可用)", text):
        stock["gaps"].add("筹码分布")
    if re.search(r"财报[^。\n]{0,30}?(缺失|N/A)", text):
        stock["gaps"].add("财报")

    confidence_match = RE_CONFIDENCE.search(text)
    if confidence_match:
        level = confidence_match.group(1) or confidence_match.group(2)
        stock["confidence"] = {"中等": "中", "偏低": "低", "偏高": "高"}.get(
            level, level
        )
    return stock


def parse_report(path):
    """Parse a full report file into {date, market_status, stocks}."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    date = None
    for line in lines:
        if line.startswith("# "):
            match = RE_TITLE_DATE.search(line)
            if match:
                date = match.group(1).replace("-", "")
            break
    if not date:
        match = RE_FILENAME_DATE.search(path.name)
        date = match.group(1) if match else ""

    market_status = None
    status_match = RE_MARKET_STATUS.search(text)
    if status_match:
        market_status = status_match.group(1).strip()

    # Split into sections by "## " headings; stock sections carry (CODE).
    sections = []
    current = None
    for line in lines:
        if line.startswith("## "):
            if current is not None:
                sections.append(current)
            heading = RE_STOCK_HEADING.match(line)
            current = (heading, [])
            continue
        if current is not None:
            current[1].append(line)
    if current is not None:
        sections.append(current)

    stocks = []
    for heading, body in sections:
        if heading is None:
            continue
        name, code = heading.group(1).strip(), heading.group(2).strip()
        stock = parse_stock_section(body)
        if stock is None:
            warn(f"{path.name}: skipped section {name} ({code}), no action line")
            continue
        stock["name"] = shorten_name(name)
        stock["code"] = code
        stocks.append(stock)
    return {"date": date, "market_status": market_status, "stocks": stocks}


def parse_market_line(reports_dir, date):
    """Build the one-line market summary from market_review_<date>.md.

    Falls back to the newest market_review_*.md in the directory, then to
    the report's own 市场状态 line. Returns None when nothing is found.
    """
    candidates = []
    if date:
        candidates.append(reports_dir / f"market_review_{date}.md")
    candidates.extend(
        sorted(reports_dir.glob("market_review_*.md"), reverse=True)
    )
    review_path = next((p for p in candidates if p.is_file()), None)
    if review_path is None:
        return None
    text = review_path.read_text(encoding="utf-8")

    quote = None
    for line in text.splitlines():
        match = re.match(r"^>\s*(今日.+?)\s*$", line)
        if match:
            quote = match.group(1)
            break
    if quote:
        head = quote.split("；", 1)[0]
        if len(head) >= 15:
            quote = head
        quote = quote.rstrip("。")

    # 结论/仓位只取“明日交易计划”一节，避免误取板块主线等处的同名表述。
    plan_start = text.find("明日交易计划")
    plan_text = text[plan_start:] if plan_start >= 0 else ""
    conclusion = None
    position = None
    plan_match = re.search(r"结论[:：]\s*([^。，；]+)", plan_text)
    if plan_match:
        conclusion = plan_match.group(1).strip()
    position_match = re.search(
        r"仓位建议控制在\s*([0-9一二三四五六七八九十—–\-~至]+成)", plan_text
    )
    if position_match:
        position = position_match.group(1)

    parts = []
    if quote:
        parts.append(quote)
    if conclusion:
        parts.append(f"结论：{conclusion}")
    if position:
        parts.append(f"仓位 {position}")
    if not parts:
        return None
    return "。".join(parts) + "。"


def parse_skipped_stocks(reports_dir, date, log_path):
    """Find stocks skipped for a market holiday, from the run log."""
    candidates = []
    if log_path is not None:
        candidates.append(Path(log_path))
    if date:
        candidates.append(
            reports_dir.parent / "logs" / f"stock_analysis_{date}.log"
        )
    for candidate in candidates:
        if candidate is None or not candidate.is_file():
            continue
        text = candidate.read_text(encoding="utf-8", errors="replace")
        matches = RE_SKIPPED.findall(text)
        if not matches:
            continue
        try:
            skipped = ast.literal_eval(matches[-1])
        except (ValueError, SyntaxError):
            continue
        if skipped:
            return sorted(str(code) for code in skipped)
    return []


def points_sentence(stock):
    """Render the entry/stop/target tail for one stock line."""
    points = stock["points"]
    action = stock["action"]
    buy, stop, target = points["buy"], points["stop"], points["target"]
    parts = []
    if action in ("买入", "加仓", "持有"):
        if buy:
            parts.append(
                f"站上 {buy} 再看" if points["buy_deferred"] else f"等回踩 {buy}"
            )
        if stop:
            parts.append(f"止损 {stop}")
        if target:
            parts.append(f"目标 {target}")
    elif action == "观望":
        if buy:
            parts.append(f"站上 {buy} 再看")
        if stop:
            parts.append(f"跌破 {stop} 止损")
        if not parts:
            parts.append("暂不买入")
    else:  # 减仓 / 卖出 / 清仓
        if target:
            parts.append(f"反弹到 {target} 附近减")
        if stop:
            parts.append(f"跌破 {stop} 止损")
        if not parts:
            parts.append("暂不操作")
    return "，".join(parts) + "。" if parts else ""


def render_simple(report, market_line, skipped, report_name):
    """Render the concise briefing Markdown."""
    stocks = report["stocks"]
    date = report["date"]
    title_date = f"{date[:4]}-{date[4:6]}-{date[6:]}" if len(date) == 8 else date

    counts = {action: 0 for action in ACTION_ORDER}
    for stock in stocks:
        counts[stock["action"]] += 1
    overview = f"**{counts['买入']} 只买入**"
    for action in ACTION_ORDER[1:]:
        if counts[action]:
            overview += f" · {action} {counts[action]}"

    out = [f"# 复盘简报 · {title_date}", ""]
    out.append(f"> 结论总览：{overview}")
    if market_line:
        line = market_line
        if line[-1] not in "。！？":
            line += "。"
        out.append(f"> 大盘：{line}")
    elif report["market_status"]:
        out.append(f"> 大盘：{report['market_status']}。")
    out.append("")
    out.append("## 个股一句话")
    out.append("")
    for stock in stocks:
        head = f"**{stock['name']}"
        if stock.get("close"):
            head += f" {stock['close']}"
            if stock.get("change"):
                head += f"（{stock['change']}）"
        head += f" · {stock['action']}**"
        reason = stock["decision"].strip()
        if reason and reason[-1] not in "。！？；…":
            reason += "。"
        out.append(f"- {head}：{reason}{points_sentence(stock)}")
    out.append("")

    reminders = []
    total = len(stocks)
    gap_counts = {}
    for gap in ("新闻", "筹码分布", "财报"):
        count = sum(1 for s in stocks if gap in s["gaps"])
        if count and count * 2 >= total:
            gap_counts[gap] = count
    if gap_counts:
        details = "，".join(
            f"{gap} {gap_counts[gap]}/{total} 只" for gap in gap_counts
        )
        reminders.append(
            f"本次{'、'.join(gap_counts)}数据普遍缺失（{details}）。"
        )
    confidences = [s["confidence"] for s in stocks if s["confidence"]]
    if confidences and (
        "低" in confidences or confidences.count("中") * 2 >= len(confidences)
    ):
        reminders.append("结论置信度以中等偏低为主，仅供参考，不构成投资建议。")
    elif not gap_counts:
        reminders.append("以上内容仅供参考，不构成投资建议。")
    if skipped:
        reminders.append(
            "以下股票因当日休市未分析："
            + "，".join(skipped)
            + "；工作日定时运行会正常包含。"
        )
    reminders.append(f"完整版报告见同目录 {report_name}。")
    out.append("## 提醒")
    out.append("")
    out.extend(f"- {item}" for item in reminders)
    out.append("")
    return "\n".join(out)


def process_report(report_path, reports_dir, log_path):
    """Generate the _simple.md briefing for one full report."""
    report_path = Path(report_path)
    if not report_path.is_file():
        print(f"error: report not found: {report_path}", file=sys.stderr)
        return False
    report = parse_report(report_path)
    if not report["stocks"]:
        print(
            f"error: no stocks parsed from {report_path.name}; "
            "report format may have changed",
            file=sys.stderr,
        )
        return False
    market_line = parse_market_line(reports_dir, report["date"])
    if market_line is None and report["market_status"]:
        market_line = report["market_status"]
    skipped = parse_skipped_stocks(reports_dir, report["date"], log_path)
    simple_name = report_path.name.replace(".md", "_simple.md")
    simple_path = report_path.with_name(simple_name)
    simple_path.write_text(
        render_simple(report, market_line, skipped, report_path.name),
        encoding="utf-8",
    )
    print(
        f"✅ 简报已生成: {simple_path}（{len(report['stocks'])} 只）"
    )
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Generate one-line concise briefings from full reports."
    )
    parser.add_argument(
        "reports",
        nargs="*",
        help="full report files (default: newest report_*.md in --reports-dir)",
    )
    parser.add_argument(
        "--reports-dir",
        default="reports",
        help="directory holding report_*.md files (default: reports)",
    )
    parser.add_argument(
        "--log",
        default=None,
        help="run log file to read skipped stocks from (optional)",
    )
    args = parser.parse_args(argv)

    reports_dir = Path(args.reports_dir)
    targets = [Path(p) for p in args.reports]
    if not targets:
        candidates = sorted(
            (p for p in reports_dir.glob("report_*.md")
             if not p.name.endswith("_simple.md")),
            key=lambda p: p.stat().st_mtime,
        )
        if not candidates:
            print(
                f"error: no report_*.md found in {reports_dir}",
                file=sys.stderr,
            )
            return 1
        targets = [candidates[-1]]

    ok = True
    for target in targets:
        if not process_report(target, reports_dir, args.log):
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
