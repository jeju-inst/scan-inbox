"""Scan Inbox 표준 CLI.

서브커맨드:
- init: 작업 폴더 및 설정 초기화
- scan: 입력 폴더 스캔, 백업, OCR, 제목 제안
- plan: 미리보기 (입력 무변경 보장, --json 지원, 사람이 읽는 출력에 OCR 본문 금지)
- apply: 승인 항목 최종 배치 및 입력 정리 (--approve-all 명시 플래그)
- rename: 검토 항목 이름 지정 및 승인
- hold: 항목 보류 처리
- undo: 최종본 되돌리기 및 원본 복구
- status: 상태 요약
- recover: 중단/장애 복구
- export-ledger: ledger.schema.json 호환 기록 출력
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from scaninbox.contracts import OcrStatus, State
from scaninbox.pipeline import Pipeline, sanitize_filename


def _resolve_paths(
    args: argparse.Namespace,
) -> tuple[Path, Path, Path]:
    """CLI 인자에서 입력, 완료, 백업 디렉터리 경로 결정."""
    input_dir = Path(args.input_dir).resolve() if args.input_dir else Path("./input").resolve()
    if args.done_dir:
        done_dir = Path(args.done_dir).resolve()
    else:
        # G1-DESIGN 기본값: 입력 폴더의 형제 scan_처리완료 또는 ./done
        done_dir = (
            input_dir.parent / f"{input_dir.name}_처리완료"
            if input_dir.name != "input"
            else Path("./done").resolve()
        )
    if args.backup_dir:
        backup_dir = Path(args.backup_dir).resolve()
    else:
        backup_dir = (
            input_dir.parent / f"{input_dir.name}_backup"
            if input_dir.name != "input"
            else Path("./backup").resolve()
        )
    return input_dir, done_dir, backup_dir


def _create_pipeline(args: argparse.Namespace) -> Pipeline:
    in_dir, done_dir, backup_dir = _resolve_paths(args)
    return Pipeline(input_dir=in_dir, done_dir=done_dir, backup_dir=backup_dir)


def cmd_init(args: argparse.Namespace, pipeline: Optional[Pipeline] = None) -> int:
    in_dir, done_dir, backup_dir = _resolve_paths(args)
    in_dir.mkdir(parents=True, exist_ok=True)
    done_dir.mkdir(parents=True, exist_ok=True)
    backup_dir.mkdir(parents=True, exist_ok=True)
    print("Scan Inbox 초기화 완료:")
    print(f"  - 입력 폴더: {in_dir}")
    print(f"  - 완료 폴더: {done_dir}")
    print(f"  - 백업 폴더: {backup_dir}")
    return 0


def cmd_scan(args: argparse.Namespace, pipeline: Optional[Pipeline] = None) -> int:
    pipe = pipeline or _create_pipeline(args)
    items = pipe.scan()
    print(f"스캔 완료: 총 {len(items)}건 발견 및 제안 생성")
    for item in items:
        status_tag = "검토필요" if item.needs_review else "승인가능"
        print(f"  [{status_tag}] {item.input_rel} -> {item.proposed_name}")
    return 0


def cmd_plan(args: argparse.Namespace, pipeline: Optional[Pipeline] = None) -> int:
    pipe = pipeline or _create_pipeline(args)
    items = pipe.plan()

    if args.json:
        out = []
        for it in items:
            out.append(
                {
                    "input": it.input_rel,
                    "original_sha256": it.original_sha256,
                    "proposed_name": it.proposed_name,
                    "ocr_status": it.ocr_status.value,
                    "needs_review": it.needs_review,
                    "title": it.title.title,
                    "doc_date": it.title.doc_date,
                    "evidence_page": it.title.evidence_page,
                    "evidence_snippet": it.title.evidence_snippet,
                    "warnings": it.warnings,
                }
            )
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    print(f"=== 처리 계획 (총 {len(items)}건) ===")
    if not items:
        print("대기 중인 처리 항목이 없습니다.")
        return 0

    for i, it in enumerate(items, 1):
        review_mark = "[검토 필요]" if it.needs_review else "[승인 대기]"
        print(f"\n{i}. {review_mark} {it.input_rel}")
        print(f"   원본 해시 : {it.original_sha256[:8]}...")
        print(f"   제안 파일명: {it.proposed_name}")
        print(f"   OCR 상태  : {it.ocr_status.value}")
        if it.title.doc_date:
            print(f"   문서 날짜 : {it.title.doc_date}")
        if it.title.evidence_snippet:
            # 사람이 읽는 출력: 오직 짧은 발췌(evidence_snippet)만 표시, 본문 전체 금지
            print(f"   제목 근거 : p.{it.title.evidence_page or 1} \"{it.title.evidence_snippet}\"")
        if it.warnings:
            print(f"   주의 사항 : {', '.join(it.warnings)}")
    return 0


def cmd_apply(args: argparse.Namespace, pipeline: Optional[Pipeline] = None) -> int:
    pipe = pipeline or _create_pipeline(args)
    targets = args.files if args.files else None
    if not targets and not args.approve_all:
        print("오류: 적용할 파일 목록을 지정하거나 --approve-all 플래그를 사용하십시오.", file=sys.stderr)
        return 1

    try:
        applied = pipe.apply(targets=targets, approve_all=args.approve_all)
        print(f"적용 완료: {len(applied)}건 완료 폴더로 배치 및 입력 정리됨")
        for f in applied:
            print(f"  - {f}")
        return 0
    except Exception as ex:
        print(f"적용 실패: {ex}", file=sys.stderr)
        return 1


def cmd_rename(args: argparse.Namespace, pipeline: Optional[Pipeline] = None) -> int:
    pipe = pipeline or _create_pipeline(args)
    try:
        entry = pipe.rename(args.file, args.new_name)
        print(f"이름 지정 완료 및 승인: {entry.input_rel} -> {entry.final_name}")
        return 0
    except Exception as ex:
        print(f"이름 지정 실패: {ex}", file=sys.stderr)
        return 1


def cmd_hold(args: argparse.Namespace, pipeline: Optional[Pipeline] = None) -> int:
    pipe = pipeline or _create_pipeline(args)
    try:
        reason = args.reason or "사용자 보류 요청"
        entry = pipe.hold(args.file, reason=reason)
        print(f"보류 처리 완료: {entry.input_rel} (사유: {reason})")
        return 0
    except Exception as ex:
        print(f"보류 처리 실패: {ex}", file=sys.stderr)
        return 1


def cmd_undo(args: argparse.Namespace, pipeline: Optional[Pipeline] = None) -> int:
    pipe = pipeline or _create_pipeline(args)
    try:
        undone = pipe.undo(input_rel=args.file)
        print(f"되돌리기 완료: {len(undone)}건 원본 복구 및 완료 파일 삭제됨")
        for f in undone:
            print(f"  - {f}")
        return 0
    except Exception as ex:
        print(f"되돌리기 실패: {ex}", file=sys.stderr)
        return 1


def cmd_status(args: argparse.Namespace, pipeline: Optional[Pipeline] = None) -> int:
    pipe = pipeline or _create_pipeline(args)
    entries = pipe.journal.list_entries()
    print("=== Scan Inbox 상태 요약 ===")
    counts: dict[str, int] = {}
    for e in entries:
        st = e.state.value if isinstance(e.state, State) else str(e.state)
        counts[st] = counts.get(st, 0) + 1

    print(f"총 등록 파일: {len(entries)}건")
    for st, c in sorted(counts.items()):
        print(f"  - {st}: {c}건")
    return 0


def cmd_recover(args: argparse.Namespace, pipeline: Optional[Pipeline] = None) -> int:
    pipe = pipeline or _create_pipeline(args)
    try:
        recovered = pipe.recover()
        print(f"복구 완료: {len(recovered)}건 복구됨")
        for f in recovered:
            print(f"  - {f}")
        return 0
    except Exception as ex:
        print(f"복구 실패: {ex}", file=sys.stderr)
        return 1


def cmd_export_ledger(args: argparse.Namespace, pipeline: Optional[Pipeline] = None) -> int:
    pipe = pipeline or _create_pipeline(args)
    ledger = pipe.export_ledger()
    ledger_json = json.dumps(ledger, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        out_p = Path(args.output).resolve()
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(ledger_json, encoding="utf-8")
        print(f"기록 저장 완료: {out_p}")
    else:
        sys.stdout.write(ledger_json)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scaninbox",
        description="Scan Inbox: 스캔 PDF 보호, OCR 추출, 제목 제안, 승인 후 배치 CLI",
    )
    # 공통 옵션
    parser.add_argument("--input-dir", "-i", help="입력 스캔 폴더 경로")
    parser.add_argument("--done-dir", "-d", help="완료 최종본 폴더 경로")
    parser.add_argument("--backup-dir", "-b", help="원본 보관 백업 폴더 경로")

    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # init
    p_init = subparsers.add_parser("init", help="작업 폴더 초기화")
    p_init.set_defaults(func=cmd_init)

    # scan
    p_scan = subparsers.add_parser("scan", help="입력 폴더 스캔 및 제안 생성")
    p_scan.set_defaults(func=cmd_scan)

    # plan
    p_plan = subparsers.add_parser("plan", help="처리 계획 미리보기 (입력 무변경)")
    p_plan.add_argument("--json", action="store_true", help="JSON 형식으로 출력")
    p_plan.set_defaults(func=cmd_plan)

    # apply
    p_apply = subparsers.add_parser("apply", help="승인 항목 확정 배치 및 입력 정리")
    p_apply.add_argument("files", nargs="*", help="적용할 파일 목록 (지정하지 않으면 승인 대기 항목 전체)")
    p_apply.add_argument("--approve-all", action="store_true", help="검토 대기 항목 일괄 승인 및 적용")
    p_apply.set_defaults(func=cmd_apply)

    # rename
    p_rename = subparsers.add_parser("rename", help="검토 항목 이름 지정 및 승인")
    p_rename.add_argument("file", help="대상 파일 상대경로")
    p_rename.add_argument("new_name", help="새 파일명 (확장자 포함 또는 제외)")
    p_rename.set_defaults(func=cmd_rename)

    # hold
    p_hold = subparsers.add_parser("hold", help="항목 보류 처리")
    p_hold.add_argument("file", help="대상 파일 상대경로")
    p_hold.add_argument("--reason", "-r", required=True, help="보류 사유")
    p_hold.set_defaults(func=cmd_hold)

    # undo
    p_undo = subparsers.add_parser("undo", help="완료 항목 되돌리기")
    p_undo.add_argument("file", nargs="?", help="되돌릴 파일 상대경로 (생략 시 전체 완료 파일)")
    p_undo.set_defaults(func=cmd_undo)

    # status
    p_status = subparsers.add_parser("status", help="상태 요약")
    p_status.set_defaults(func=cmd_status)

    # recover
    p_recover = subparsers.add_parser("recover", help="중단 및 장애 복구")
    p_recover.set_defaults(func=cmd_recover)

    # export-ledger
    p_ledger = subparsers.add_parser("export-ledger", help="reconcile 호환 ledger JSON 출력")
    p_ledger.add_argument("-o", "--output", help="출력 파일 경로 (생략 시 stdout)")
    p_ledger.set_defaults(func=cmd_export_ledger)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
