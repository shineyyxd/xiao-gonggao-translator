# -*- coding: utf-8 -*-
"""管线薄入口：解析 CLI → 调 Orchestrator（编排逻辑全部在 orchestrator.py）。

用法：
  python main.py --date 2026-08-26 --mock-llm --source sample  # 样本重放（回归用）
  python main.py --date 2026-08-27 --mock-llm --source live    # 实时抓取跑一期
  python main.py --date 2026-08-27 --source live --no-tts      # 跳过音频
  python main.py --date 2026-08-27 --source live --push        # 触发微信推送
退出码：0 = 正常/EMPTY_DAY；2 = 停刊（STOPPED）。
"""
import argparse
import logging
import sys
from datetime import date as date_cls

# 兼容旧导入路径（tests 等从 main 导入这两个函数）
from orchestrator import Orchestrator, build_output_item, process_item  # noqa: F401

__all__ = ["Orchestrator", "build_output_item", "process_item", "main"]


def main(argv=None):
    parser = argparse.ArgumentParser(description="小公告翻译官（银发向）管线（多Agent+确定性编排）")
    parser.add_argument("--date", default=date_cls.today().isoformat(),
                        help="运行日期，默认今天（样本数据覆盖 2026-08-25~08-26）")
    parser.add_argument("--source", choices=["sample", "live"], default="sample",
                        help="数据源：sample=本地研发样本（回归用），live=巨潮实时抓取")
    parser.add_argument("--mock-llm", action="store_true",
                        help="强制 mock 模式（无 API key 时自动开启）")
    parser.add_argument("--no-tts", action="store_true", help="跳过音频合成")
    parser.add_argument("--push", action="store_true", help="触发微信推送")
    parser.add_argument("--window-days", type=int, default=5,
                        help="公告日期窗口（当日及前 N-1 个自然日），默认 5")
    parser.add_argument("--ignore-memory", action="store_true",
                        help="跳过 Memory 跨天去重（重放历史日期时用）")
    parser.add_argument("--pdf-summary", action=argparse.BooleanOptionalAction,
                        default=None,
                        help="抓入选公告 PDF 提取原文摘录补摘要（默认 live 开、sample 关）")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s",
                        stream=sys.stderr)

    orch = Orchestrator(run_date=args.date, source=args.source,
                        mock_llm=args.mock_llm, no_tts=args.no_tts,
                        do_push=args.push, window_days=args.window_days,
                        ignore_memory=args.ignore_memory,
                        pdf_summary=args.pdf_summary)
    summary = orch.run()

    print(f"\n===== {args.date} 期管线结束（status={summary['status']}，run_id={summary['run_id']}）=====")
    if summary["status"] == "STOPPED":
        print(f"停刊原因：{summary['reason']}")
        return summary["exit_code"]
    if summary["status"] == "EMPTY_DAY":
        print("今日无重要公告（简版路径，无音频）")
        return summary["exit_code"]
    print(f"模式：{'mock' if summary['mock'] else '真实 LLM'}（数据源：{args.source}）")
    print(f"选题 {len(summary['picks'])} 条，成稿 {len(summary['items'])} 条："
          f"{ '、'.join(i['company'] for i in summary['items']) }")
    print(f"事实准确率：{summary['accuracy'] * 100:.1f}%"
          f"（{summary['checkpoints_passed']}/{summary['checkpoints_total']} 核对点）")
    print(f"产出目录：{summary['out_dir']}")
    print(f"  - {summary['json_path'].name} / {summary['html_path'].name} / 日报.md")
    print(f"  - audio/ 音频 {len(summary['audio_paths'])} 条")
    print(f"  - audit_{summary['run_id']}.jsonl（审计留痕）")
    if args.push and summary.get("push_result"):
        r = summary["push_result"]
        state = "成功" if r["ok"] else ("跳过（未配置 webhook）" if r["skipped"] else "失败已落盘待人工推送")
        print(f"微信推送：{state}")
    return summary["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
