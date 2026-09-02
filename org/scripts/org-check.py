#!/usr/bin/env python3
"""タスク台帳の停滞検知と整合性検査。

AI開発組織のタスク台帳（索引の CSV ファイルと、タスクごとの Markdown
ファイル）を読み、次の3つを出力する。

  1. 停滞候補       — 更新が止まっているタスク
  2. リマインド候補 — PO（人間のプロジェクトオーナー）の回答を長く待っているタスク
  3. 整合性の警告   — 索引と詳細ファイルの食い違い、書式違反

判定するだけで、対処はしない。何をするかはオーケストレーターが決める。
スクリプトが勝手に担当を変えると、なぜそうなったかが記録に残らないため。

Python 3.8 以降。標準ライブラリのみ。追加インストールは要らない。

使い方:
    python3 org-check.py                  検査する（既定）
    python3 org-check.py --summary        ゴール健全性の指標を集計する
    python3 org-check.py --statusline     1行にまとめる（ステータス行向け）
    python3 org-check.py --days 3         停滞と判定する日数を変える（既定 2）
    python3 org-check.py --root path/to/repo
    python3 org-check.py --hook             セッション開始フックから呼ぶとき

`python3` という名前のコマンドが無い環境（Windows の標準的な導入ではこれが普通）
では `python` に読み替える。どちらが使えるかは実行環境ごとに違うため、
セッション開始フックの設定（settings.snippet.json）は両方を順に試す形にしてある。

終了コード:
    0  検出なし
    1  停滞候補または警告あり
    2  台帳が読めない、または書式が壊れている

--hook を付けると、検出があっても 0 で終わる。Claude Code のセッション開始
フックは、終了コードが 0 のときだけ標準出力を読み込む仕様のため——そのまま
だと、検出があるときほど結果が届かなくなる。
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import glob
import os
import re
import statistics
import sys

# Windows のコンソールや、呼び出し元がパイプで受け取る場面では、Python の
# 既定の出力文字コードが cp932 になる。この組織の出力は日本語で、記号（em
# dash 等）を含むため、そのままだと UnicodeEncodeError で**処理そのものが
# 落ちる**。落ちたことはセッション開始フックの中では見えないので、
# 「走っているつもりで走っていない」状態になる。
# 呼び出し側（フック、パイプ、CI）はいずれも UTF-8 で読むため、出力を
# UTF-8 に固定する。文字化けと異常終了の両方がこれで消える。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # 差し替え済み / 閉じている場合
        pass


# --- 台帳の書式（.claude/rules/org-task-ledger.md と一致させること） ---

COLUMNS = ["ID", "作成日", "更新日", "タスク名", "状態", "優先度", "担当", "依存タスク", "ドキュメント"]

STATES = [
    "未着手", "設計中", "テスト作成中", "実装中",
    "テスト中", "レビュー中", "PO確認待ち", "完了", "保留", "中止",
]
TERMINAL = {"完了", "中止"}          # 動かないのが正しい状態
IN_PROGRESS = {"設計中", "テスト作成中", "実装中", "テスト中", "レビュー中"}
PRIORITIES = {"高", "中", "低"}

# 「担当」がこれらの値なら、オーケストレーター自身が抱えているとみなす。
# 工程が進行中なのにこうなっていれば、委譲を怠っている。
ORCHESTRATOR_NAMES = {"オーケストレーター", "orchestrator", "org-orchestrator", "メインセッション"}

UNASSIGNED = "未割当"
NONE_MARK = "無し"
UNKNOWN_MARK = "不明"

# 手戻りが何回続いたら、収束していないとみなすか。
# レビューとテストの反復上限（2往復）から導いた値。
REWORK_LIMIT = 3

# 指示として埋まっていなければならない節。空のまま割り当てると、レビューとテストが
# 判定基準を持たないまま動く。人間の目視に任せず機械で捕まえる。
REQUIRED_SECTIONS = ["完了条件", "判断してよい範囲", "変更範囲", "禁止事項"]

# 雛形の案内文（HTMLコメント）と、埋めたつもりの空文字。どちらも「未記入」とみなす。
HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
PLACEHOLDERS = {"", "-", "—", "tbd", "todo", "未定", "未記入", "（未記入）", "(未記入)"}

# PO確認待ちキューの状態。見出しの行に識別子と一緒に置く決まり。
QUEUE_STATES = ["未回答", "回答済み", "取り下げ"]

TASK_ID = re.compile(r"T-\d+")
# 「T-001（ブロッカー）」「T-001（推奨: 理由）」。全角と半角のかっこを両方許す。
DEP_ENTRY = re.compile(r"(T-\d+)\s*[（(]([^）)]*)[)）]")
Q_ID = re.compile(r"Q-\d+")
META_LINE = re.compile(r"^\s*[-*]\s*([^:：]+)\s*[:：]\s*(.*?)\s*$")


class LedgerError(Exception):
    """台帳そのものが読めない。終了コード 2 になる。"""


# --------------------------------------------------------------------------
# 読み込み
# --------------------------------------------------------------------------

def find_index(root: str) -> str | None:
    """索引の CSV ファイルを探す。まだ無ければ None（組織が未着手）。"""
    hits = sorted(glob.glob(os.path.join(root, "docs", "task-list-*.csv")))
    if not hits:
        return None
    if len(hits) > 1:
        raise LedgerError(
            "索引の CSV が複数ある。1つに絞ること:\n  "
            + "\n  ".join(os.path.relpath(h, root) for h in hits)
        )
    return hits[0]


def read_index(path: str) -> list[dict]:
    # utf-8-sig にしておくと、表計算ソフトが付ける先頭の目印があっても読める。
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
    except OSError as e:
        raise LedgerError(f"索引の CSV を開けない: {e}") from e
    except csv.Error as e:
        raise LedgerError(f"索引の CSV の書式が壊れている: {e}") from e

    if not rows:
        return []
    missing = [c for c in COLUMNS if c not in rows[0]]
    if missing:
        raise LedgerError(
            "索引の CSV に必要な列が無い: " + " / ".join(missing)
            + "\n見出し行はこの9列にすること: " + ",".join(COLUMNS)
        )
    return [{(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in rows]


def read_doc(path: str) -> str:
    """タスク別ファイルの中身。読めなければ空文字。"""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def read_metadata(text: str) -> dict:
    """タスク別ファイルの「## メタデータ」から `- キー: 値` を読む。"""
    meta, inside = {}, False
    for line in text.splitlines():
        if line.startswith("#"):
            # 見出しに入ったらメタデータ節は終わり
            inside = line.strip().lstrip("#").strip() == "メタデータ"
            continue
        if inside:
            m = META_LINE.match(line)
            if m:
                meta[m.group(1).strip()] = m.group(2).strip()
    meta["_blockers"] = Q_ID.findall(section(text, "ブロッカー"))
    return meta


def is_blank(body: str) -> bool:
    """節が実質的に未記入か。雛形の案内文（HTMLコメント）だけなら未記入とみなす。"""
    return HTML_COMMENT.sub("", body).strip().lower() in PLACEHOLDERS


def section(text: str, name: str) -> str:
    """見出し `## name` から次の見出しまでの本文を返す。"""
    out, inside = [], False
    for line in text.splitlines():
        if line.startswith("#"):
            inside = line.strip().lstrip("#").strip() == name
            continue
        if inside:
            out.append(line)
    return "\n".join(out)


def parse_date(value: str):
    if not value or value == UNKNOWN_MARK:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


# 依存の注記。英語表記は task-cycle 方式からの移行で現れる。
# 読み違えると、着手できないはずのタスクが着手可能に見えるため両方受ける。
BLOCKER_WORDS = ("ブロッカー", "blocker")
ADVISORY_WORDS = ("推奨", "recommend")


def classify_dep(note: str) -> bool | None:
    """依存の注記を判定する。True=ブロッカー / False=推奨 / None=判定不能。"""
    low = note.lower()
    if any(w in low for w in BLOCKER_WORDS):
        return True
    if any(w in low for w in ADVISORY_WORDS):
        return False
    return None


def parse_deps(value: str) -> list[tuple[str, bool | None]]:
    """依存の欄を (タスクID, ブロッカーか) の一覧にする。

    3番目の状態 None は「注記があるが、ブロッカーとも推奨とも読めない」。
    黙って推奨として扱うと、着手できないはずのタスクが動き出すため、
    判定不能はそのまま返して呼び出し側に警告させる。
    """
    if not value or value == NONE_MARK:
        return []
    found = [(tid, classify_dep(note)) for tid, note in DEP_ENTRY.findall(value)]
    if found:
        return found
    # かっこ無しで書かれている場合。書式違反だが、IDは拾って安全側（ブロッカー）に倒す。
    return [(tid, True) for tid in TASK_ID.findall(value)]


def count_open_questions(root: str) -> int | None:
    """PO確認待ちキューの未回答件数。ファイルが無ければ None。"""
    path = os.path.join(root, "docs", "po-queue.md")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    # 「Q-001」と「未回答」が同じ行にある件数を数える
    return sum(1 for line in text.splitlines() if Q_ID.search(line) and "未回答" in line)


# --------------------------------------------------------------------------
# 検査
# --------------------------------------------------------------------------

def build(root: str, index_path: str, today: dt.date) -> list[dict]:
    """索引の各行に、詳細ファイルの中身と経過日数を足したものを作る。"""
    tasks = []
    for row in read_index(index_path):
        doc_rel = row.get("ドキュメント", "")
        doc_abs = os.path.join(root, doc_rel) if doc_rel else ""
        doc_text = read_doc(doc_abs) if doc_abs and os.path.isfile(doc_abs) else ""
        meta = read_metadata(doc_text) if doc_text else {}
        updated = parse_date(row.get("更新日", ""))

        try:
            rework = int(meta.get("手戻り回数", "0") or 0)
        except ValueError:
            rework = 0

        tasks.append({
            "row": row,
            "id": row.get("ID", ""),
            "name": row.get("タスク名", ""),
            "state": row.get("状態", ""),
            "owner": row.get("担当", ""),
            "priority": row.get("優先度", ""),
            "deps": parse_deps(row.get("依存タスク", "")),
            "doc_rel": doc_rel,
            "doc_exists": bool(doc_abs) and os.path.isfile(doc_abs),
            "meta": meta,
            "text": doc_text,
            "rework": rework,
            "updated": updated,
            "age": (today - updated).days if updated else None,
        })
    return tasks


def stagnation_exempt(task: dict, state_of: dict) -> str | None:
    """組織側の停滞として数えない理由。数えるべきものには None を返す。

    停滞判定（check）と滞留日数の集計（summarize）は、必ずこの同じ判定を通す。
    片方だけが除外を持っていると、組織が手を打っても動かないタスクが
    「詰まっている工程」として報告され、指示の足りているタスクへ指示を足す、
    という誤った対処に進む。実際にそれが起きたため関数に切り出してある。

    依存の注記が判定不能（classify_dep が None を返す）のときは、
    ブロッカーとして扱う（`blocking is not False`）。推奨と誤読して
    着手させるより、ブロッカーと誤読して止める方が損が小さい。
    注記そのものへの警告は check() が別に出す。
    """
    state = task["state"]
    if state in TERMINAL:
        return "終端（完了・中止）"
    if state == "保留":
        return "意図して止めている"
    if state == "PO確認待ち":
        return "PO の回答待ち。組織側の停滞ではない"
    if state == "未着手" and any(
        blocking is not False and state_of.get(dep_id) not in TERMINAL
        for dep_id, blocking in task["deps"]
    ):
        return "依存先待ち。連鎖の根元だけを見ればよい"
    return None


def check_queue(root: str) -> list[str]:
    """PO確認待ちキューの書式検査。識別子と状態が同じ行に無いものを警告する。"""
    path = os.path.join(root, "docs", "po-queue.md")
    if not os.path.exists(path):
        return []
    text = read_doc(path)
    if not text:
        return []

    seen, with_state = set(), set()
    for line in text.splitlines():
        ids = set(Q_ID.findall(line))
        if not ids:
            continue
        seen |= ids
        if any(st in line for st in QUEUE_STATES):
            with_state |= ids

    missing = sorted(seen - with_state)
    if not missing:
        return []
    return [
        f"PO確認待ちキュー: {', '.join(missing)} の状態が識別子と同じ行に無い"
        "。未回答の件数が数えられず、指標に出ない（`### Q-001 [未回答] 要約` の形にする）"
    ]


def check(tasks: list[dict], days: int) -> tuple[list, list, list]:
    """(停滞候補, リマインド候補, 整合性の警告) を返す。"""
    stale, remind, warn = [], [], []
    known = {t["id"] for t in tasks if t["id"]}
    state_of = {t["id"]: t["state"] for t in tasks}

    for t in tasks:
        row, tid, state = t["row"], t["id"], t["state"]
        label = f"{tid or '(ID無し)'} {t['name']}".strip()

        # --- 書式 ---
        if not tid or not TASK_ID.fullmatch(tid):
            warn.append(f"{label}: ID が `T-###` の形になっていない")
        if state not in STATES:
            warn.append(f"{label}: 状態「{state or '(空欄)'}」は定義された10種のどれでもない")
        if t["priority"] and t["priority"] not in PRIORITIES:
            warn.append(f"{label}: 優先度「{t['priority']}」は 高/中/低 のどれでもない")

        for col in ("作成日", "更新日", "依存タスク"):
            v = row.get(col, "")
            if not v or v == "-":
                warn.append(f"{label}: {col} が空欄。無いなら「{NONE_MARK}」、不明なら「{UNKNOWN_MARK}」と書く")
        if row.get("更新日") and t["updated"] is None and row["更新日"] != UNKNOWN_MARK:
            warn.append(f"{label}: 更新日「{row['更新日']}」が YYYY-MM-DD になっていない")

        # --- 詳細ファイル ---
        if not t["doc_rel"]:
            warn.append(f"{label}: ドキュメント列が空欄")
        elif not t["doc_exists"]:
            warn.append(f"{label}: 詳細ファイルが無い → {t['doc_rel']}")
        else:
            meta = t["meta"]
            # `_` で始まるキーはスクリプトが足した内部用。実際に書かれた項目だけを見る。
            if not any(not k.startswith("_") for k in meta):
                warn.append(f"{label}: 詳細ファイルに「## メタデータ」節が無いか、`- キー: 値` の形になっていない")
            else:
                if meta.get("状態") and meta["状態"] != state:
                    warn.append(
                        f"{label}: 状態が食い違う（索引「{state}」/ 詳細「{meta['状態']}」）"
                        "。担当が実際に作業した記録がある詳細側を真とする"
                    )
                # 索引が空欄のときは既に別の警告を出しているので、重ねて言わない
                if meta.get("更新日") and row.get("更新日") and meta["更新日"] != row.get("更新日"):
                    warn.append(
                        f"{label}: 更新日が食い違う（索引「{row.get('更新日')}」/ 詳細「{meta['更新日']}」）"
                        "。索引が古いと停滞を誤検知する"
                    )

            # --- 指示として埋まっていなければならない節 ---
            # 割り当て済み、または着手済みのタスクだけを見る。中止は対象外。
            assigned = t["owner"] and t["owner"] != UNASSIGNED
            if state != "中止" and (assigned or state in IN_PROGRESS or state == "完了"):
                empty = [n for n in REQUIRED_SECTIONS if is_blank(section(t["text"], n))]
                if empty:
                    # 引き金は「担当が付いている」だけではない。**担当が未割当
                    # でも、状態が着手済み（設計中〜レビュー中）か「完了」なら
                    # 出る。** 別方式の台帳から移してきた直後は担当が全件未割当
                    # になるので、そこを「担当が付いている」と書くと、読んだ側
                    # が台帳を見て食い違いに悩むことになる。
                    trigger = ("担当が付いている" if assigned
                               else f"状態が「{state}」になっている")
                    warn.append(
                        f"{label}: 指示が未記入のまま{trigger} → {' / '.join(empty)}"
                        "。レビューとテストが判定基準を持たないまま動く"
                    )
            if state == "完了" and is_blank(section(t["text"], "証拠")):
                warn.append(
                    f"{label}: 状態が「完了」なのに `## 証拠` が空。"
                    "完了条件を満たしたと言える根拠（実行したコマンドと実出力）が無い"
                )

        # --- 依存 ---
        for dep_id, blocking in t["deps"]:
            if dep_id not in known:
                warn.append(f"{label}: 依存先 {dep_id} が索引に無い")
            if blocking is None:
                warn.append(
                    f"{label}: 依存 {dep_id} の注記が「ブロッカー」とも「推奨」とも読めない。"
                    "`T-XXX（ブロッカー）` か `T-XXX（推奨: 理由）` と書く"
                    "（安全側でブロッカーとして扱っている）"
                )

        # --- 担当 ---
        if state not in TERMINAL:
            if not t["owner"] or t["owner"] == UNASSIGNED:
                if state != "未着手":
                    warn.append(f"{label}: 状態「{state}」なのに担当が未割当。誰も動かしていない")
            elif t["owner"] in ORCHESTRATOR_NAMES and state in IN_PROGRESS:
                warn.append(
                    f"{label}: 状態「{state}」の担当がオーケストレーターになっている。"
                    "委譲を怠っている可能性がある"
                )

        # --- 停滞・リマインド ---
        if t["age"] is None:
            continue
        overdue = t["age"] >= days

        if state == "PO確認待ち":
            # 停滞には数えないが、PO への通知としては出す。
            if overdue:
                qs = ", ".join(t["meta"].get("_blockers", [])) or "Q-ID の記載無し"
                remind.append(f"{label}: {t['age']}日 未回答（{qs}）")
            continue

        if stagnation_exempt(t, state_of):
            continue

        if overdue:
            stale.append(f"{label} [{state} / {t['owner'] or UNASSIGNED}] "
                         f"最終更新 {row.get('更新日')}（{t['age']}日）— 実時間基準")
        if t["rework"] >= REWORK_LIMIT:
            stale.append(f"{label} [{state}] 手戻り {t['rework']}回 — サイクル基準")

    return stale, remind, warn


# --------------------------------------------------------------------------
# 集計（ゴール健全性の指標）
# --------------------------------------------------------------------------

def summarize(tasks: list[dict], days: int, open_questions: int | None) -> dict:
    by_state = {}
    for t in tasks:
        by_state.setdefault(t["state"], []).append(t)

    done = len(by_state.get("完了", []))
    open_tasks = [t for t in tasks if t["state"] not in TERMINAL]
    stale, _, _ = check(tasks, days)

    state_of = {t["id"]: t["state"] for t in tasks}
    blocked = [
        t for t in open_tasks
        if t["state"] == "保留"
        or any(b is not False and state_of.get(d) not in TERMINAL for d, b in t["deps"])
    ]

    # 滞留日数は「組織が手を打てば動くもの」だけで測る。停滞判定と同じ除外を通す。
    ages = {}
    for state in [x for x in STATES if x not in TERMINAL] + [x for x in by_state if x not in STATES]:
        vals = [
            t["age"] for t in by_state.get(state, [])
            if t["age"] is not None and stagnation_exempt(t, state_of) is None
        ]
        if vals:
            ages[state] = statistics.median(vals)

    return {
        "完了": done,
        "未完了": len(open_tasks),
        "停滞": len(stale),
        "ブロック中": len(blocked),
        "PO確認待ち(タスク)": len(by_state.get("PO確認待ち", [])),
        "PO確認待ち(質問)": open_questions,
        "手戻り": sum(t["rework"] for t in tasks),
        # 台帳の状態順に並べる。定義外の状態も末尾に残す——黙って消すと
        # 合計が未完了数と合わなくなり、タスクが1件行方不明になる。
        "工程別": {
            st: len(by_state[st])
            for st in [x for x in STATES if x not in TERMINAL]
                      + [x for x in by_state if x not in STATES]
            if by_state.get(st)
        },
        "滞留日数の中央値": ages,
        "残作業": [f"{t['id']} {t['name']} [{t['state']}]" for t in open_tasks],
    }


# --------------------------------------------------------------------------
# 出力
# --------------------------------------------------------------------------

def print_checks(stale, remind, warn, days) -> int:
    if not (stale or remind or warn):
        print(f"タスク台帳: 停滞なし・警告なし（停滞の基準 {days}日）")
        return 0

    if stale:
        print(f"■ 停滞候補 {len(stale)}件（{days}日以上更新なし、または手戻り{REWORK_LIMIT}回以上）")
        for s in stale:
            print(f"  - {s}")
        print("  → 原因を確認 → 再分割 → 別の担当へ → それでも進まなければ PO へ。")
        print("    同じタスクが2回目の停滞なら、途中を飛ばして PO へ上げる。")
        print()
    if remind:
        print(f"■ PO への リマインド候補 {len(remind)}件")
        for r in remind:
            print(f"  - {r}")
        print("  → 組織側の停滞ではない。PO へ通知する。")
        print()
    if warn:
        print(f"■ 台帳の整合性 警告 {len(warn)}件")
        for w in warn:
            print(f"  - {w}")
        print()
    return 1


def print_summary(s: dict) -> int:
    print("■ ゴール健全性の指標")
    print(f"  完了 {s['完了']} / 未完了 {s['未完了']} / 停滞 {s['停滞']} / ブロック中 {s['ブロック中']}")
    q = s["PO確認待ち(質問)"]
    print(f"  PO確認待ち: タスク {s['PO確認待ち(タスク)']}件 / 質問 "
          f"{q if q is not None else 'キュー未作成'}")
    print(f"  手戻り 累計 {s['手戻り']}回")
    if s["工程別"]:
        print("  工程別: " + " / ".join(f"{k} {v}" for k, v in s["工程別"].items()))
    if s["滞留日数の中央値"]:
        print("  滞留日数の中央値: " + " / ".join(f"{k} {v:.0f}日" for k, v in s["滞留日数の中央値"].items()))
        # 「保留」と「PO確認待ち」は意図して止めているので、詰まりの候補から外す。
        # 組織が手を打っても動かないものを工程の詰まりと呼ぶと、判断を誤らせる。
        active = {k: v for k, v in s["滞留日数の中央値"].items() if k in IN_PROGRESS or k == "未着手"}
        if active:
            worst = max(active.items(), key=lambda kv: kv[1])
            print(f"  → 詰まっている工程の候補: {worst[0]}（滞留の中央値 {worst[1]:.0f}日）")
    print()
    print("  残作業:")
    for line in s["残作業"] or ["（無し）"]:
        print(f"    - {line}")
    print()
    print("  ここまでが機械的な集計。「前進している / 停滞している / ゴールが揺らいでいる」の")
    print("  判定と、その理由は、オーケストレーターが書く。完了数が増えているのに残作業が")
    print("  減っていないなら、ゴールの分解を疑うこと。")
    return 0


def print_statusline(s: dict, stale_count: int) -> int:
    parts = [f"完了 {s['完了']}/{s['完了'] + s['未完了']}"]
    # 作業中の工程だけを出す。未着手・保留・PO確認待ちは工程ではないので混ぜない。
    working = " ".join(f"{k}{v}" for k, v in s["工程別"].items() if k in IN_PROGRESS)
    if working:
        parts.append(working)
    if stale_count:
        parts.append(f"停滞{stale_count}")
    if s["PO確認待ち(タスク)"]:
        parts.append(f"PO待ち{s['PO確認待ち(タスク)']}")
    if s["手戻り"]:
        parts.append(f"手戻り{s['手戻り']}")
    print("[org] " + " · ".join(parts))
    return 0


# --------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="タスク台帳の停滞検知と整合性検査",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--root", default=".", help="リポジトリのルート（既定: カレントディレクトリ）")
    ap.add_argument("--days", type=int, default=2, help="停滞と判定する日数（既定: 2）")
    ap.add_argument("--today", help="基準日 YYYY-MM-DD（既定: 実行日）")
    ap.add_argument("--hook", action="store_true",
                    help="セッション開始フックから呼ぶ。検出があっても終了コード 0 を返す")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--summary", action="store_true", help="ゴール健全性の指標を集計する")
    mode.add_argument("--statusline", action="store_true", help="1行にまとめる")
    args = ap.parse_args(argv)

    if args.days < 1:
        print("--days は1以上にすること", file=sys.stderr)
        return 2

    today = dt.date.today()
    if args.today:
        try:
            today = dt.date.fromisoformat(args.today)
        except ValueError:
            print(f"--today は YYYY-MM-DD で指定すること: {args.today}", file=sys.stderr)
            return 2

    try:
        index_path = find_index(args.root)
        if index_path is None:
            # まだ組織が動き出していない。セッション開始を妨げないよう、静かに終わる。
            if not args.statusline:
                print("タスク台帳がまだ無い（docs/task-list-*.csv）。"
                      "最初のタスクを登録する前に、オーケストレーターが作る。")
            return 0
        tasks = build(args.root, index_path, today)
    except LedgerError as e:
        message = f"台帳を読めない: {e}"
        if args.hook:
            # Claude Code は終了コードが 0 のときだけ標準出力をセッションの文脈へ入れる。
            # 台帳が壊れていることこそオーケストレーターへ届けたい情報なので、
            # 標準エラーではなく標準出力へ出し、0 で終わる。
            print(message)
            return 0
        print(message, file=sys.stderr)
        return 2

    if not tasks:
        if not args.statusline:
            print(f"タスク台帳は空（{os.path.relpath(index_path, args.root)}）")
        return 0

    if args.summary or args.statusline:
        s = summarize(tasks, args.days, count_open_questions(args.root))
        return print_summary(s) if args.summary else print_statusline(s, s["停滞"])

    stale, remind, warn = check(tasks, args.days)
    warn += check_queue(args.root)
    code = print_checks(stale, remind, warn, args.days)
    # フックから呼ばれたときは 0 を返す。Claude Code は終了コードが 0 のときだけ
    # 標準出力をセッションの文脈へ入れるため、1 を返すと検出結果そのものが届かない。
    return 0 if args.hook else code


if __name__ == "__main__":
    sys.exit(main())
