#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 网关压测 HTML 报告渲染模块。"""

from __future__ import annotations

import html as html_lib
import math
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


SCRIPT_VERSION = "v2"
DEFAULT_MODEL = "deepseek-v4-flash-ga-260731"


def as_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def avg(values: Iterable[float]) -> float:
    items = [v for v in values if v is not None and math.isfinite(v)]
    if not items:
        return 0.0
    return sum(items) / len(items)


def rate(ok: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return ok * 100.0 / total


def ms(value: Optional[float]) -> str:
    if value is None or not math.isfinite(value):
        return "-"
    return f"{value:.0f}ms"


def grade_order(grade: str) -> int:
    return {"A": 1, "B": 2, "C": 3, "D": 4, "E": 5}.get(str(grade).upper(), 5)


class HtmlReportRenderer:
    def write(self, data: Dict[str, Any], output: Path) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(self.render(data), encoding="utf-8")

    def render(self, data: Dict[str, Any]) -> str:
        meta = data.get("meta") or {}
        title = f"{meta.get('model', DEFAULT_MODEL)} 基准测试报告 {SCRIPT_VERSION}"
        body = "\n".join(
            [
                self._summary(data),
                self._nav(),
                '<div class="wrap">',
                self._section_env(data),
                self._section_api(data),
                self._section_concurrency(data),
                self._section_context(data),
                self._section_token(data),
                self._section_consistency(data),
                self._section_admission(data),
                self._section_conclusion(data),
                "</div>",
            ]
        )
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<style>
{self._css()}
</style>
</head>
<body>
<header class="hero">
  <div class="hero-inner">
    <p class="eyebrow">AI Gateway Benchmark Report · {escape(SCRIPT_VERSION)}</p>
    <h1>{escape(title)}</h1>
    <p class="desc">OpenAI 兼容网关的接口能力、并发压力、上下文长度、Token/缓存和一致性测试结果。</p>
  </div>
</header>
{body}
</body>
</html>
"""

    def _summary(self, data: Dict[str, Any]) -> str:
        meta = data.get("meta") or {}
        admission = data.get("admission") or {}
        api_rows = data.get("api_rows") or []
        conc_rows = data.get("concurrency_rows") or []
        context_summary = admission.get("context_summary") or []
        total_requests = sum(as_int(row.get("requests")) for row in conc_rows)
        total_success = sum(as_int(row.get("success")) for row in conc_rows)
        overall_success = rate(total_success, total_requests) if total_requests else 0.0
        context_bad = [f"{row.get('context')}({row.get('grade')})" for row in context_summary if grade_order(row.get("grade", "E")) > 2]
        return f"""
<div class="summary">
  {self._kpi("整体成功率", f"{overall_success:.1f}%", "ok" if overall_success >= 98 else "bad")}
  {self._kpi("API 通过率", f"{sum(1 for row in api_rows if row.get('ok'))}/{len(api_rows)}" if api_rows else "跳过", "ok" if not api_rows or admission.get("api_rate", 100) >= 80 else "bad")}
  {self._kpi("准入判定", "准入" if admission.get("admission_ok") else "不准入", "ok" if admission.get("admission_ok") else "bad")}
  {self._kpi("未达 B 级项", ", ".join(context_bad) if context_bad else "无", "bad" if context_bad else "ok")}
  {self._kpi("资源竞争", str((admission.get("competition") or {}).get("severity", "未知")), "bad" if str((admission.get("competition") or {}).get("severity")) in {"中", "高"} else "ok")}
  {self._kpi("测试模型", str(meta.get("model", "-")), "")}
</div>
"""

    def _nav(self) -> str:
        items = [
            ("sec-env", "测试环境"),
            ("sec-api", "API 接入能力"),
            ("sec-conc", "并发压力测试"),
            ("sec-ctx", "上下文长度分级"),
            ("sec-token", "Token 指标"),
            ("sec-cons", "模型能力一致性"),
            ("sec-adm", "性能准入基线"),
            ("sec-conclusion", "综合结论与建议"),
        ]
        links = "".join(f'<a href="#{target}">{label}</a>' for target, label in items)
        return f'<nav class="nav">{links}</nav>'

    def _section_env(self, data: Dict[str, Any]) -> str:
        meta = data.get("meta") or {}
        module_times = meta.get("module_times") or {}
        module_time_text = "<br>".join(f"{escape(k)} = {escape(v)}" for k, v in module_times.items()) or "-"
        rows = [
            ("模型", meta.get("model", "-")),
            ("API 端点", meta.get("endpoint", "-")),
            ("脚本版本", meta.get("script_version", SCRIPT_VERSION)),
            ("生成时间", meta.get("finished_at", "-")),
            ("各模块测试时间", module_time_text),
            ("总耗时", f"{meta.get('duration_seconds', 0)} 秒"),
            ("并发档位", ", ".join(str(i) for i in meta.get("concurrency_levels", []))),
            ("每档请求数", meta.get("requests_per_level", "-")),
            ("上下文档位", ", ".join(meta.get("context_levels", []))),
            ("思考模式扫描", f"{meta.get('reasoning_scan')} / {meta.get('reasoning_effort')}"),
            ("重试策略", f"retries={meta.get('retries')}，backoff={meta.get('backoff')}s"),
            ("超时", f"timeout={meta.get('timeout')}s，long_timeout={meta.get('long_timeout')}s"),
        ]
        table = self._kv_table(rows)
        return self._card("sec-env", "测试环境与方法学", "样本量 / 时间 / 配置", table)

    def _section_api(self, data: Dict[str, Any]) -> str:
        rows = data.get("api_rows") or []
        if not rows:
            return self._card("sec-api", "API 接入能力", "已跳过", '<p class="hint">本次未执行 API 接入能力测试。</p>')
        api_ok = sum(1 for row in rows if row.get("ok"))
        headers = ["测试项", "结果", "状态码", "延迟", "TTFT", "详情"]
        table_rows = [
            [
                row.get("name"),
                self._badge(row.get("ok")),
                row.get("status") or "-",
                ms(float(row.get("latency_ms") or 0)),
                ms(row.get("ttft_ms")),
                escape(row.get("detail") or ""),
            ]
            for row in rows
        ]
        content = (
            '<div class="kpi-row">'
            + self._kpi("API 通过率", f"{api_ok}/{len(rows)}", "ok" if rate(api_ok, len(rows)) >= 80 else "bad")
            + self._kpi("通过率", f"{rate(api_ok, len(rows)):.1f}%", "ok" if rate(api_ok, len(rows)) >= 80 else "bad")
            + "</div>"
            + self._table(headers, table_rows)
        )
        return self._card("sec-api", "API 接入能力", "接口实例 / 错误案例 / 响应契约校验", content)

    def _section_concurrency(self, data: Dict[str, Any]) -> str:
        rows = data.get("concurrency_rows") or []
        headers = ["并发", "成功率", "均延迟", "P50", "P90", "P99", "吞吐(req/s)"]
        main_rows = [
            [
                row.get("concurrency"),
                f"{float(row.get('success_rate') or 0.0):.1f}%",
                ms(row.get("avg_latency_ms")),
                ms(row.get("p50_ms")),
                ms(row.get("p90_ms")),
                ms(row.get("p99_ms")),
                f"{float(row.get('throughput_rps') or 0.0):.2f}",
            ]
            for row in rows
        ]
        detail_headers = ["并发", "TTFT均", "TTFT_P50", "TTFT_P90", "TTFT_P99", "TPOT", "基础设施错误率", "业务错误率"]
        detail_rows = [
            [
                row.get("concurrency"),
                ms(row.get("avg_ttft_ms")),
                ms(row.get("ttft_p50_ms")),
                ms(row.get("ttft_p90_ms")),
                ms(row.get("ttft_p99_ms")),
                ms(row.get("avg_tpot_ms")),
                f"{float(row.get('infra_error_rate') or 0.0):.1f}%",
                f"{float(row.get('business_error_rate') or 0.0):.1f}%",
            ]
            for row in rows
        ]
        competition = ((data.get("admission") or {}).get("competition") or {})
        content = (
            self._table(headers, main_rows)
            + "<h3>TTFT / TPOT / 错误率</h3>"
            + self._table(detail_headers, detail_rows)
            + "<h3>延迟分位数趋势</h3>"
            + svg_line_chart(rows, [("p50_ms", "P50"), ("p90_ms", "P90"), ("p99_ms", "P99")], "concurrency")
            + "<h3>吞吐趋势</h3>"
            + svg_bar_chart(rows, "throughput_rps", "concurrency")
            + f'<div class="alert {"bad" if competition.get("severity") in {"中", "高"} else "ok"}">资源竞争严重度：<strong>{escape(competition.get("severity", "未知"))}</strong>　{escape(competition.get("detail", ""))}</div>'
        )
        return self._card("sec-conc", "并发压力测试", "延迟 / TTFT / TPOT / 吞吐 / 错误率", content)

    def _section_context(self, data: Dict[str, Any]) -> str:
        rows = data.get("context_rows") or []
        if not rows:
            return self._card("sec-ctx", "上下文长度分级", "已跳过", '<p class="hint">本次未执行上下文长度测试。</p>')
        headers = ["上下文", "估算tokens", "reasoning", "等级", "成功率", "TTFT_P50", "TTFT_P90", "错误率", "诊断"]
        table_rows = [
            [
                row.get("context"),
                row.get("estimated_tokens"),
                row.get("reasoning"),
                self._grade(row.get("grade")),
                f"{float(row.get('success_rate') or 0.0):.1f}%",
                ms(row.get("ttft_p50_ms")),
                ms(row.get("ttft_p90_ms")),
                f"{float(row.get('error_rate') or 0.0):.1f}%",
                escape(row.get("diagnosis") or ""),
            ]
            for row in rows
        ]
        content = self._table(headers, table_rows)
        return self._card("sec-ctx", "上下文长度分级", "A-E 分级 + 失败根因诊断", content)

    def _section_token(self, data: Dict[str, Any]) -> str:
        token_rows = data.get("token_rows") or []
        cache_rows = data.get("cache_rows") or []
        cache_summary = data.get("cache_summary") or {}
        if not token_rows and not cache_rows:
            return self._card("sec-token", "Token 指标", "已跳过", '<p class="hint">本次未执行 Token/缓存测试。</p>')

        ok_token_rows = [row for row in token_rows if row.get("ok")]
        avg_otps = avg([float(row.get("otps") or 0.0) for row in ok_token_rows])
        peak_otps = max([float(row.get("otps") or 0.0) for row in ok_token_rows] or [0.0])
        min_otps = min([float(row.get("otps") or 0.0) for row in ok_token_rows] or [0.0])

        token_headers = ["结果", "max_tokens", "prompt", "completion", "cached", "pcht", "命中率", "OTPS", "延迟"]
        token_table_rows = [
            [
                self._badge(row.get("ok")),
                row.get("max_tokens"),
                row.get("prompt_tokens"),
                row.get("completion_tokens"),
                row.get("cached_tokens"),
                row.get("prompt_cache_hit_tokens"),
                f"{float(row.get('cache_hit_rate') or 0.0):.1f}%",
                f"{float(row.get('otps') or 0.0):.1f} tok/s",
                ms(row.get("latency_ms")),
            ]
            for row in token_rows
        ]
        cache_headers = ["序号", "结果", "prompt", "completion", "cached", "pcht", "命中", "延迟", "错误"]
        cache_table_rows = [
            [
                row.get("index"),
                self._badge(row.get("ok")),
                row.get("prompt_tokens"),
                row.get("completion_tokens"),
                row.get("cached_tokens"),
                row.get("prompt_cache_hit_tokens"),
                "是" if row.get("hit") else "否",
                ms(row.get("latency_ms")),
                escape(row.get("error") or ""),
            ]
            for row in cache_rows
        ]

        conc_rows = data.get("concurrency_rows") or []
        token_conc_headers = ["并发", "成功请求", "输出token", "墙钟(s)", "token/s(墙钟口径)"]
        token_conc_rows = [
            [
                row.get("concurrency"),
                row.get("success"),
                row.get("completion_tokens"),
                f"{float(row.get('wall_seconds') or 0.0):.2f}",
                f"{float(row.get('token_per_sec_wall') or 0.0):.2f}",
            ]
            for row in conc_rows
        ]

        content = (
            '<div class="kpi-row">'
            + self._kpi("平均 OTPS", f"{avg_otps:.1f}", "ok")
            + self._kpi("峰值 OTPS", f"{peak_otps:.1f}", "ok")
            + self._kpi("最低 OTPS", f"{min_otps:.1f}", "ok" if min_otps > 0 else "bad")
            + self._kpi("缓存命中", f"{cache_summary.get('hits', 0)}/{cache_summary.get('total', 0)}", "ok" if cache_summary.get("passed") else "bad")
            + "</div>"
            + "<h3>OTPS / Usage 明细</h3>"
            + self._table(token_headers, token_table_rows)
            + "<h3>缓存命中率（重复完全相同长前缀请求）</h3>"
            + f'<p class="hint">{escape(cache_summary.get("note", ""))}</p>'
            + self._table(cache_headers, cache_table_rows)
            + "<h3>Token 吞吐随并发变化</h3>"
            + self._table(token_conc_headers, token_conc_rows)
        )
        return self._card("sec-token", "Token 指标", "OTPS / Usage 统计 / 缓存命中率", content)

    def _section_consistency(self, data: Dict[str, Any]) -> str:
        rows = data.get("consistency_rows") or []
        if not rows:
            return self._card("sec-cons", "模型能力一致性", "已跳过", '<p class="hint">本次未执行一致性测试。</p>')
        ok = sum(1 for row in rows if row.get("consistent"))
        headers = ["问题", "重复", "成功", "关键词命中", "相似度", "一致", "样例"]
        table_rows = [
            [
                escape(row.get("question") or ""),
                row.get("repeat"),
                row.get("success"),
                f"{float(row.get('keyword_hit_rate') or 0.0):.1f}%",
                f"{float(row.get('similarity') or 0.0):.2f}",
                self._badge(row.get("consistent")),
                escape(row.get("sample") or ""),
            ]
            for row in rows
        ]
        content = (
            '<div class="kpi-row">'
            + self._kpi("整体一致率", f"{rate(ok, len(rows)):.1f}%", "ok" if rate(ok, len(rows)) >= 70 else "bad")
            + self._kpi("准入判定", "通过" if rate(ok, len(rows)) >= 70 else "未通过", "ok" if rate(ok, len(rows)) >= 70 else "bad")
            + "</div>"
            + self._table(headers, table_rows)
        )
        return self._card("sec-cons", "模型能力一致性", "关键词硬门槛 + 相似度辅助", content)

    def _section_admission(self, data: Dict[str, Any]) -> str:
        admission = data.get("admission") or {}
        checks = admission.get("checks") or []
        headers = ["检查项", "状态", "结果/说明"]
        table_rows = [
            [
                row.get("name"),
                self._badge_kind(row.get("status"), row.get("kind")),
                escape(row.get("detail") or ""),
            ]
            for row in checks
        ]
        verdict_class = "ok" if admission.get("admission_ok") else "bad"
        content = (
            f'<div class="verdict {verdict_class}">准入判定：{"建议准入" if admission.get("admission_ok") else "暂不建议准入"}　（标准：上下文需达 B 级，API≥80%，一致性≥70%，并发成功率≥98%）</div>'
            + self._table(headers, table_rows)
        )
        return self._card("sec-adm", "性能准入基线", "方案 B：达到 B 级即准入", content)

    def _section_conclusion(self, data: Dict[str, Any]) -> str:
        meta = data.get("meta") or {}
        admission = data.get("admission") or {}
        cache_summary = data.get("cache_summary") or {}
        competition = admission.get("competition") or {}
        context_summary = admission.get("context_summary") or []
        below_b = [row for row in context_summary if grade_order(row.get("grade", "E")) > 2]
        cache_text = (
            f"缓存命中 {cache_summary.get('hits', 0)}/{cache_summary.get('total', 0)}；{cache_summary.get('note', '')}"
            if cache_summary
            else "本次未执行缓存测试。"
        )
        context_text = (
            "上下文全部达到 B 级。"
            if not below_b
            else "上下文未达 B 项：" + "，".join(f"{row.get('context')}={row.get('grade')}" for row in below_b) + "。"
        )
        snapshot_rows = [
            ["模型", escape(meta.get("model", "-")), "本次测试对象"],
            ["网关端点", escape(meta.get("endpoint", "-")), "OpenAI 兼容 chat/completions"],
            ["整体成功率", f"{float(admission.get('overall_success') or 0.0):.1f}%", "并发压测汇总"],
            ["吞吐峰值", f"{float(admission.get('peak_throughput') or 0.0):.2f} req/s @ c={admission.get('peak_concurrency')}", escape(competition.get("severity", "未知"))],
            ["上下文分级", escape(context_text), "准入关键项"],
            ["缓存命中", escape(cache_text), "信息项，建议与官方直连对比"],
        ]
        advice_rows = [
            ["高", "缓存字段持续为 0", "确认上游实际 usage 是否含 prompt_cache_hit_tokens 或 prompt_tokens_details.cached_tokens；同时确认网关是否透传。"],
            ["中", "高并发吞吐倒挂", "把业务限流放在峰值吞吐对应并发附近；超过该并发需要扩网关实例或上游通道。"],
            ["中", "TTFT 接近阈值", "用官方直连和网关各跑一轮，分离模型首 token 延迟与网关转发延迟。"],
        ]
        content = (
            f'<div class="conc-quote"><b>{escape(meta.get("model", "-"))}</b> 本次综合判定为 '
            f'<b>{"建议准入" if admission.get("admission_ok") else "暂不建议准入"}</b>。'
            f'资源竞争严重度为 <b>{escape(competition.get("severity", "未知"))}</b>；'
            f'{escape(context_text)} {escape(cache_text)}</div>'
            "<h3>关键数字快照</h3>"
            + self._table(["指标", "数值", "评价"], snapshot_rows)
            + "<h3>风险评估与建议</h3>"
            + self._table(["严重度", "风险", "建议"], advice_rows)
        )
        return self._card("sec-conclusion", "综合结论与建议", "自动归因和落地建议", content)

    def _card(self, section_id: str, title: str, subtitle: str, content: str) -> str:
        return f"""
<section class="card" id="{section_id}">
  <div class="card-head">
    <h2>{escape(title)}</h2>
    <p class="sub">{escape(subtitle)}</p>
  </div>
  {content}
</section>
"""

    def _table(self, headers: Sequence[Any], rows: Sequence[Sequence[Any]]) -> str:
        head = "".join(f"<th>{escape(value)}</th>" for value in headers)
        if not rows:
            body = f'<tr><td colspan="{len(headers)}" class="muted">无数据</td></tr>'
        else:
            body = "".join("<tr>" + "".join(f"<td>{value}</td>" for value in row) + "</tr>" for row in rows)
        return f'<div class="tbl-wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'

    def _kv_table(self, rows: Sequence[Tuple[str, Any]]) -> str:
        body = "".join(
            f'<tr><td class="kv-key"><b>{escape(key)}</b></td><td>{value if isinstance(value, str) and "<br>" in value else escape(value)}</td></tr>'
            for key, value in rows
        )
        return f'<div class="tbl-wrap"><table class="kv"><tbody>{body}</tbody></table></div>'

    def _kpi(self, label: str, value: Any, kind: str = "") -> str:
        return f'<div class="kpi"><div class="kpi-v {kind}">{escape(value)}</div><div class="kpi-l">{escape(label)}</div></div>'

    def _badge(self, ok: Any) -> str:
        return '<span class="badge ok">通过</span>' if ok else '<span class="badge bad">未通过</span>'

    def _badge_kind(self, label: Any, kind: Any) -> str:
        kind = str(kind or "muted")
        return f'<span class="badge {escape(kind)}">{escape(label)}</span>'

    def _grade(self, grade: Any) -> str:
        grade_text = str(grade or "-").upper()
        cls = "ok" if grade_text in {"A", "B"} else "bad"
        return f'<span class="grade {cls}">{escape(grade_text)}</span>'

    def _css(self) -> str:
        return """
:root {
  color-scheme: light;
  --bg: #f6f8fb;
  --panel: #ffffff;
  --ink: #0f172a;
  --muted: #64748b;
  --line: #e2e8f0;
  --blue: #2563eb;
  --green: #16a34a;
  --red: #dc2626;
  --amber: #d97706;
  --teal: #0f766e;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; line-height: 1.55; }
.hero { background: #0f172a; color: #fff; padding: 44px 24px 36px; }
.hero-inner { max-width: 1180px; margin: 0 auto; }
.eyebrow { margin: 0 0 10px; color: #93c5fd; font-size: 13px; letter-spacing: .04em; text-transform: uppercase; }
h1 { margin: 0; font-size: 34px; line-height: 1.18; font-weight: 750; }
.desc { max-width: 760px; margin: 12px 0 0; color: #cbd5e1; }
.summary { max-width: 1180px; margin: -24px auto 18px; padding: 0 24px; display: grid; grid-template-columns: repeat(6, minmax(130px, 1fr)); gap: 12px; }
.kpi { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 16px; min-height: 82px; box-shadow: 0 1px 2px rgba(15, 23, 42, .04); }
.kpi-v { font-size: 22px; font-weight: 760; color: var(--ink); overflow-wrap: anywhere; }
.kpi-v.ok { color: var(--green); }
.kpi-v.bad { color: var(--red); }
.kpi-l { margin-top: 5px; color: var(--muted); font-size: 12px; }
.nav { max-width: 1180px; margin: 0 auto 18px; padding: 0 24px; display: flex; flex-wrap: wrap; gap: 8px; }
.nav a { color: #1d4ed8; background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 6px; padding: 7px 10px; text-decoration: none; font-size: 13px; }
.wrap { max-width: 1180px; margin: 0 auto 48px; padding: 0 24px; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 22px; margin: 18px 0; box-shadow: 0 1px 2px rgba(15, 23, 42, .04); }
.card-head { display: flex; align-items: baseline; justify-content: space-between; gap: 16px; border-bottom: 1px solid var(--line); padding-bottom: 12px; margin-bottom: 16px; }
h2 { margin: 0; font-size: 22px; }
h3 { margin: 24px 0 10px; font-size: 16px; }
.sub { color: var(--muted); margin: 0; font-size: 13px; }
.kpi-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 16px; }
.tbl-wrap { overflow-x: auto; margin: 10px 0; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 10px 11px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
th { color: #334155; background: #f8fafc; font-weight: 700; }
td { color: #1e293b; }
.kv { max-width: 860px; }
.kv-key { width: 180px; color: #334155; background: #f8fafc; }
.badge, .grade { display: inline-block; border-radius: 999px; padding: 2px 8px; font-size: 12px; font-weight: 700; white-space: nowrap; }
.badge.ok, .grade.ok { color: #166534; background: #dcfce7; }
.badge.bad, .grade.bad { color: #991b1b; background: #fee2e2; }
.badge.info { color: #1d4ed8; background: #dbeafe; }
.badge.muted { color: #475569; background: #e2e8f0; }
.verdict { padding: 14px 16px; border-radius: 8px; font-weight: 760; margin-bottom: 14px; }
.verdict.ok, .alert.ok { color: #14532d; background: #dcfce7; border: 1px solid #86efac; }
.verdict.bad, .alert.bad { color: #7f1d1d; background: #fee2e2; border: 1px solid #fecaca; }
.alert { margin: 14px 0; padding: 12px 14px; border-radius: 8px; }
.hint, .muted { color: var(--muted); }
.conc-quote { background: #f8fafc; border-left: 4px solid var(--blue); padding: 14px 16px; border-radius: 6px; margin-bottom: 16px; }
svg { background: #fff; border: 1px solid var(--line); border-radius: 8px; margin: 8px 0 14px; }
@media (max-width: 900px) {
  .summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .card-head { display: block; }
}
@media (max-width: 560px) {
  .summary { grid-template-columns: 1fr; }
  h1 { font-size: 28px; }
  .wrap, .summary, .nav { padding-left: 14px; padding-right: 14px; }
  .card { padding: 16px; }
}
"""


def escape(value: Any) -> str:
    return html_lib.escape(str(value))


def svg_line_chart(
    rows: List[Dict[str, Any]],
    keys: List[Tuple[str, str]],
    x_key: str,
    width: int = 920,
    height: int = 260,
) -> str:
    if not rows:
        return '<p class="hint">无趋势数据。</p>'

    colors = ["#2563eb", "#ea580c", "#16a34a", "#0f766e", "#9333ea"]
    pad_l, pad_r, pad_t, pad_b = 54, 18, 28, 38
    chart_w = width - pad_l - pad_r
    chart_h = height - pad_t - pad_b
    values = [float(row.get(key) or 0.0) for row in rows for key, _ in keys]
    max_value = max(values or [1.0]) or 1.0

    def x_at(index: int) -> float:
        if len(rows) == 1:
            return pad_l + chart_w / 2
        return pad_l + chart_w * index / (len(rows) - 1)

    def y_at(value: float) -> float:
        return pad_t + chart_h - chart_h * value / max_value

    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}">']
    for i in range(5):
        y = pad_t + chart_h * i / 4
        label = max_value * (1 - i / 4)
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" stroke="#e2e8f0"/>')
        parts.append(f'<text x="{pad_l - 8}" y="{y + 3:.1f}" font-size="10" fill="#64748b" text-anchor="end">{label:.0f}</text>')

    for index, row in enumerate(rows):
        x = x_at(index)
        parts.append(f'<text x="{x:.1f}" y="{height - 12}" font-size="10" fill="#64748b" text-anchor="middle">{escape(row.get(x_key))}</text>')

    for series_index, (key, label) in enumerate(keys):
        color = colors[series_index % len(colors)]
        points = " ".join(f"{x_at(i):.1f},{y_at(float(row.get(key) or 0.0)):.1f}" for i, row in enumerate(rows))
        parts.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2"/>')
        for i, row in enumerate(rows):
            parts.append(f'<circle cx="{x_at(i):.1f}" cy="{y_at(float(row.get(key) or 0.0)):.1f}" r="3" fill="{color}"/>')
        legend_x = pad_l + series_index * 110
        parts.append(f'<rect x="{legend_x}" y="8" width="12" height="12" fill="{color}" rx="2"/>')
        parts.append(f'<text x="{legend_x + 18}" y="18" font-size="11" fill="#475569">{escape(label)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def svg_bar_chart(
    rows: List[Dict[str, Any]],
    key: str,
    x_key: str,
    width: int = 920,
    height: int = 240,
) -> str:
    if not rows:
        return '<p class="hint">无趋势数据。</p>'

    pad_l, pad_r, pad_t, pad_b = 54, 18, 28, 38
    chart_w = width - pad_l - pad_r
    chart_h = height - pad_t - pad_b
    values = [float(row.get(key) or 0.0) for row in rows]
    max_value = max(values or [1.0]) or 1.0
    slot = chart_w / max(len(rows), 1)
    bar_w = min(slot * 0.56, 54)
    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}">']

    for i in range(5):
        y = pad_t + chart_h * i / 4
        label = max_value * (1 - i / 4)
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" stroke="#e2e8f0"/>')
        parts.append(f'<text x="{pad_l - 8}" y="{y + 3:.1f}" font-size="10" fill="#64748b" text-anchor="end">{label:.2f}</text>')

    for index, row in enumerate(rows):
        value = float(row.get(key) or 0.0)
        x_center = pad_l + slot * index + slot / 2
        h = chart_h * value / max_value
        x = x_center - bar_w / 2
        y = pad_t + chart_h - h
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{h:.1f}" fill="#2563eb" rx="3"/>')
        parts.append(f'<text x="{x_center:.1f}" y="{height - 12}" font-size="10" fill="#64748b" text-anchor="middle">{escape(row.get(x_key))}</text>')
        parts.append(f'<text x="{x_center:.1f}" y="{max(y - 5, 12):.1f}" font-size="10" fill="#475569" text-anchor="middle">{value:.2f}</text>')

    parts.append("</svg>")
    return "".join(parts)


