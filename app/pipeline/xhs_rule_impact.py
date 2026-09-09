from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from typing import Any

import httpx


VERDICTS = {"no_change", "review", "action_required"}
EXTERNAL_LINK_PATTERN = re.compile(r"https?://[^\s<>\"'，。]+")
MANUAL_LINK_CHECK = "打开该链接核对类目可售明细 / 店铺类型可售范围"
SYSTEM_PROMPT = (
    "你是小红书电商合规分析助手，根据规则变化和商家在售商品判断影响，只输出 JSON。"
    "重点检查类目下线、类目转定向准入、店铺类型可售范围收紧，以及虚拟商品、电子资源、"
    "教育、激活码相关条款。证据不足不得猜测。"
)
ImpactCaller = Callable[[str, str], Awaitable[str]]


def fallback_analysis(deadline: str = "") -> dict[str, Any]:
    return {
        "verdict": "review",
        "summary": "AI 分析失败，请人工核对在售商品",
        "affected_items": [],
        "action_plan": ["人工阅读规则正文并逐一核对在售商品"],
        "manual_checks": [],
        "deadline": deadline,
    }


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(row).strip() for row in value if str(row).strip()]


def normalise_analysis(
    payload: Any, shop_snapshot: dict[str, Any], external_links: list[str], deadline: str,
) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("verdict") not in VERDICTS:
        result = fallback_analysis(deadline)
    else:
        affected = payload.get("affected_items")
        affected_items: list[dict[str, str]] = []
        if isinstance(affected, list):
            for row in affected:
                if not isinstance(row, dict):
                    continue
                risk = str(row.get("risk") or "medium").casefold()
                affected_items.append({
                    "item_id": str(row.get("item_id") or ""),
                    "title": str(row.get("title") or ""),
                    "category": str(row.get("category") or ""),
                    "risk": risk if risk in {"high", "medium", "low"} else "medium",
                    "why": str(row.get("why") or ""),
                    "suggestion": str(row.get("suggestion") or ""),
                })
        result = {
            "verdict": payload["verdict"],
            "summary": str(payload.get("summary") or "").strip(),
            "affected_items": affected_items,
            "action_plan": _strings(payload.get("action_plan")),
            "manual_checks": _strings(payload.get("manual_checks")),
            "deadline": str(payload.get("deadline") or deadline),
        }

    if external_links:
        for link in external_links:
            check = f"{MANUAL_LINK_CHECK}：{link}"
            if check not in result["manual_checks"]:
                result["manual_checks"].append(check)
        if result["verdict"] == "no_change":
            result["verdict"] = "review"
            result["summary"] = "规则含外部类目明细，需人工核对后才能确认是否影响在售商品"

    items = shop_snapshot.get("items") if isinstance(shop_snapshot, dict) else []
    stale_or_empty = not isinstance(items, list) or not items or bool(shop_snapshot.get("stale"))
    if stale_or_empty:
        if result["verdict"] == "no_change":
            result["verdict"] = "review"
        note = "商品信息过期/未拉取到，需人工核对"
        if note not in result["summary"]:
            result["summary"] = f"{result['summary']}；{note}".strip("；")
        if note not in result["manual_checks"]:
            result["manual_checks"].append(note)
    if not result["summary"]:
        result["summary"] = "需人工核对规则变化对在售商品的影响"
    result["deadline"] = result["deadline"] or deadline
    return result


class DeepSeekRuleImpactAnalyzer:
    def __init__(self, caller: ImpactCaller | None = None) -> None:
        self.caller = caller or self._call_deepseek

    async def analyze(self, rule: dict[str, Any], shop_snapshot: dict[str, Any]) -> dict[str, Any]:
        external_links = [
            link.rstrip(".,") for link in rule.get("external_links", []) if str(link).startswith("http")
        ]
        deadline = str(rule.get("effective_at") or "")
        user_prompt = self._prompt(rule, shop_snapshot)
        try:
            raw = await self.caller(SYSTEM_PROMPT, user_prompt)
            match = re.search(r"\{.*\}", raw.strip(), re.S)
            if not match:
                raise ValueError("DeepSeek response has no JSON object")
            parsed = json.loads(match.group(0))
        except Exception:
            parsed = fallback_analysis(deadline)
        return normalise_analysis(parsed, shop_snapshot, external_links, deadline)

    @staticmethod
    def _prompt(rule: dict[str, Any], shop_snapshot: dict[str, Any]) -> str:
        schema = {
            "verdict": "no_change | review | action_required",
            "summary": "一句话结论",
            "affected_items": [{
                "item_id": "", "title": "", "category": "", "risk": "high|medium|low",
                "why": "", "suggestion": "",
            }],
            "action_plan": ["可执行步骤，带时间点"],
            "manual_checks": ["程序读不到、需人工核对的项"],
            "deadline": "生效日期",
        }
        return (
            "请比较规则变化并逐个核对在售商品。只输出符合下列结构的 JSON：\n"
            f"{json.dumps(schema, ensure_ascii=False)}\n"
            "若正文出现腾讯文档或外部表格，必须加入 manual_checks，不能猜测 no_change。"
            "商品快照 stale 或为空时 verdict 至少 review。\n"
            f"规则：{json.dumps(rule, ensure_ascii=False)}\n"
            f"在售商品快照：{json.dumps(shop_snapshot, ensure_ascii=False)}"
        )

    @staticmethod
    async def _call_deepseek(system: str, user: str) -> str:
        key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        if not key:
            raise RuntimeError("DEEPSEEK_API_KEY is required")
        endpoint = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/") + "/chat/completions"
        model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
        last_error: Exception | None = None
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            for attempt in range(3):
                try:
                    response = await client.post(
                        endpoint,
                        headers={"Authorization": f"Bearer {key}"},
                        json={
                            "model": model,
                            "messages": [
                                {"role": "system", "content": system},
                                {"role": "user", "content": user},
                            ],
                            "temperature": 0.1,
                            "response_format": {"type": "json_object"},
                        },
                    )
                    response.raise_for_status()
                    return str(response.json()["choices"][0]["message"]["content"])
                except (httpx.HTTPError, KeyError, IndexError, TypeError) as exc:
                    last_error = exc
                    if attempt < 2:
                        await asyncio.sleep(0.5 * (2**attempt))
        raise RuntimeError(f"DeepSeek impact analysis failed: {last_error}")


def _urgent(effective_at: str, within_days: int, today: date | None = None) -> bool:
    try:
        effective = datetime.strptime(effective_at[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return False
    remaining = (effective - (today or date.today())).days
    return -1 <= remaining <= within_days


def format_impact_notification(
    name: str, old_announced: str, new_announced: str, effective_at: str,
    analysis: dict[str, Any], item_count: int, *, urgent_within_days: int = 7,
    manual: bool = False, today: date | None = None,
) -> tuple[str, str, str]:
    marker = "【手动触发】" if manual else ""
    verdict = analysis.get("verdict")
    if verdict == "no_change":
        title = f"{marker}《{name}》在售商品影响分析"
        summary = (
            f"【小红书规则】《{name}》有更新，但不影响你在售的 {item_count} 个商品，无需改动。\n"
            f"公示 {old_announced}→{new_announced} ｜ 生效 {effective_at}"
        )
        return title, summary, "normal"

    label = "🔴需要改动" if verdict == "action_required" else "🟡建议核对"
    urgent = f"（距今不足{urgent_within_days}天，紧急）" if _urgent(effective_at, urgent_within_days, today) else ""
    affected = analysis.get("affected_items") or []
    lines = [
        f"【小红书规则影响分析】{marker}{label}",
        f"规则《{name}》更新 ｜ 公示 {old_announced}→{new_announced} ｜ 生效 {effective_at}{urgent}",
        f"在售 {item_count} 个，可能受影响 {len(affected)} 个：",
    ]
    risk_labels = {"high": "高", "medium": "中", "low": "低"}
    for row in affected[:10]:
        lines.append(
            f"· 「{row.get('title') or row.get('item_id') or '未命名商品'}」"
            f"{risk_labels.get(row.get('risk'), '中')}风险：{row.get('why') or '需核对'}"
            f" → {row.get('suggestion') or '人工复核'}"
        )
    actions = analysis.get("action_plan") or []
    if actions:
        lines.append("待办：")
        lines.extend(f"{index}. {value}" for index, value in enumerate(actions, 1))
    checks = analysis.get("manual_checks") or []
    if checks:
        lines.append("需人工核对：")
        lines.extend(f"· {value}" for value in checks)
    lines.append(f"截止：{analysis.get('deadline') or effective_at or '未标注'}")
    return f"{marker}{name} · 在售商品影响", "\n".join(lines), "highest" if verdict == "action_required" else "normal"
