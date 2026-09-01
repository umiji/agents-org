#!/usr/bin/env python3
"""決定ログの索引を、タスク別ファイルから生成する。

## なぜこれが要るか

**決定ログの目的は「再燃防止」である** —— 同じ論点が別のタスクで、しばしば
違う結論で、また争われるのを防ぐこと。担当エージェント同士は自由に連絡できず、
会話履歴も引き継がないため、会話の中にしか無い理由はセッションが終われば
組織から永久に失われる。

**再燃防止が目的である以上、引けることが機能の本体である。** ところが決定は
タスク別ファイル（`docs/tasks/T-XXX.md`）の中に埋まっており、タスクは決定の
主題ではなく作業が起きた順に並んでいる。「認証ライブラリをなぜこれにしたか」
を調べたい側にとって、その決定は「ログイン画面の実装」という名前のタスクの中に
ある。しかも完了したタスクは、規約上そもそも読み返されない。

**つまり、書く費用だけがかかって効果が0だった。** このスクリプトはその索引を
作る。走査と並べ替えに判断は要らないので、LLM にやらせない（トークンを消費した
うえに、通すたびに内容が変わる）。

## 正典と生成物

  正典    docs/tasks/T-XXX.md の `## 決定ログ`。担当エージェントが自分の
          タスクのファイルへ追記する。**過去の項目は書き換えない**
  生成物  docs/decisions-index.md。**このスクリプトだけが書く。手で書かない**

**決定が失効したことは、索引の上でだけ表す。** 新しい決定の側に
`- 上書き対象: T-003 2026-05-10` と書かせ、このスクリプトが古い側の行の状態を
「置き換え済み → T-021」に変える。過去のタスク別ファイルは一文字も触らない。
外の既成解（ADR ツール）は古いファイルの状態欄を書き換えるが、本組織の
「過去を書き換えず新項目を追記する」規約と衝突するため、索引側に寄せてある。

Python 3.8 以降。標準ライブラリのみ。追加インストールは要らない。

使い方:
    python3 org-decisions.py                    索引を作り直す
    python3 org-decisions.py --check            書き込まず、古くなっていないかだけ見る
    python3 org-decisions.py --root path/to/repo  対象リポジトリ（既定: カレント）
    python3 org-decisions.py --hook             フックから呼ぶとき

`python3` という名前のコマンドが無い環境（Windows の標準的な導入ではこれが
普通）では `python` に読み替える。

--hook を付けると、何が起きても 0 で終わり、出力を数行に抑える。セッション開始
時の自動実行で、組織を動かしていないリポジトリを騒がせないためである。

終了コード:
    0  生成した（または、決定が1件も無いので何もしなかった）
    1  --check で、索引が古い・欠けていると分かった
"""

from __future__ import annotations

import argparse
import os
import re
import sys

# --- 置き場 ---------------------------------------------------------------

TASKS_DIR = os.path.join("docs", "tasks")
INDEX = os.path.join("docs", "decisions-index.md")

# --- 索引の形 -------------------------------------------------------------

TITLE = "# 決定ログ索引"

# 表の列。`対象` は見出しになるので、表そのものには入れない。
COLUMNS = ["日付", "要約", "決定", "タスク", "状態"]

GENERATED_NOTE = (
    "> **このファイルは生成物である。手で書かない。書き換えても次の生成で消える。**\n"
    "> 決定の正典は各タスクの `docs/tasks/T-XXX.md` にある `## 決定ログ` のほうで、\n"
    "> このファイルは `org-decisions.py`（決定ログ索引の生成スクリプト）が作り直す。"
)

ACTIVE = "有効"
SUPERSEDED = "置き換え済み → {}"
UNCLASSIFIED = "未分類"

# 索引は「開くべき1件を決める」ためのものなので、決定の中身は切り詰める。全文は
# タスク別ファイルにある。**読む費用が決定の件数にしか比例しない**ことが、この
# 索引の判定基準の1つだったためである。
#
# 対象は1〜3語なので、この上限に実際に当たるのは `決定` の欄だけである。
SUMMARY_LIMIT = 120

# --- 決定ログの書式 -------------------------------------------------------

# 「### YYYY-MM-DD 一行でわかる決定の要約」
HEADING = re.compile(r"^\s{0,3}#{3,6}\s+(.*\S)\s*$")
DATE_HEAD = re.compile(r"^(\d{4}-\d{2}-\d{2})\s*[-—:：]?\s*(.*)$")
# 「- 対象: 認証」。全角のコロンも許す。
META_LINE = re.compile(r"^\s*[-*]\s*([^:：]+?)\s*[:：]\s*(.*?)\s*$")
# 「T-003 2026-05-10」
SUPERSEDE_REF = re.compile(r"(T-\d+)\s+(\d{4}-\d{2}-\d{2})")

HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)

TASK_FILE = re.compile(r"^(T-\d+)\.md$")


# --------------------------------------------------------------------------
# 読み込み
# --------------------------------------------------------------------------

def task_files(root: str) -> list:
    """タスク別ファイルを、タスク番号の数の順で返す。

    **完了・中止のタスクも含める。** 「完了済みは読まない」という規約こそが、
    決定が引けなくなっていた原因だったためである。
    """
    d = os.path.join(root, TASKS_DIR)
    try:
        names = os.listdir(d)
    except OSError:
        return []
    found = []
    for name in names:
        m = TASK_FILE.match(name)
        path = os.path.join(d, name)
        if m and os.path.isfile(path):
            found.append((int(m.group(1)[2:]), m.group(1), path))
    found.sort()
    return [(tid, path) for _, tid, path in found]


def read_text(path: str) -> str:
    """読めなければ空文字。1本読めないことで索引全体を落とさない。"""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def decision_log(text: str) -> str:
    """`## 決定ログ` の本文を返す。中の `###` 見出しは本文の一部として残す。

    第2レベル（`##`）の見出しで区切る。決定の1件ずつが `###` なので、見出しなら
    何でも切る作りにすると中身が消える。
    """
    out, inside = [], False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("##") and not stripped.startswith("###"):
            inside = stripped.lstrip("#").strip() == "決定ログ"
            continue
        if inside:
            out.append(line)
    return "\n".join(out)


def flatten(value: str) -> str:
    """CSV の1セルに収まる形へ。改行を畳み、長ければ切り詰める。"""
    value = " ".join(value.split())
    if len(value) > SUMMARY_LIMIT:
        value = value[:SUMMARY_LIMIT] + "…"
    return value


def parse_decisions(task_id: str, body: str, warn: list) -> list:
    """決定ログの本文から、決定を1件ずつ取り出す。

    雛形の案内文（HTML コメント）は先に落とす。残っているだけの節を決定として
    数えると、索引が雛形で埋まる。
    """
    body = HTML_COMMENT.sub("", body)
    decisions, current = [], None

    def close():
        if current is not None:
            decisions.append(current)

    for line in body.splitlines():
        head = HEADING.match(line)
        if head:
            close()
            current = None
            title = head.group(1)
            m = DATE_HEAD.match(title)
            if not m:
                warn.append(
                    "{}: 決定の見出しに日付が無い（`### YYYY-MM-DD 要約` の形で書く）: {}"
                    .format(task_id, flatten(title))
                )
                continue
            current = {
                "タスク": task_id,
                "日付": m.group(1),
                "要約": flatten(m.group(2)) or "（要約なし）",
                "対象": "",
                "決定": "",
                "上書き対象": "",
            }
            continue
        if current is None:
            continue
        meta = META_LINE.match(line)
        if meta:
            key, value = meta.group(1).strip(), meta.group(2).strip()
            if key in ("対象", "決定", "上書き対象"):
                current[key] = value
        elif line.strip() and current["決定"] and not current["対象"]:
            # 「- 決定: 一行目」の続きが字下げで書かれている場合を拾う
            current["決定"] += " " + line.strip()

    close()

    for d in decisions:
        if not d["対象"]:
            warn.append(
                "{} {}: `- 対象:` が無い。`{}` として索引へ入れた（対象は索引を束ねる単位であり、"
                "語をまだ知らない側が辿り着くための入口である）"
                .format(d["タスク"], d["日付"], UNCLASSIFIED)
            )
            d["対象"] = UNCLASSIFIED
        if not d["決定"]:
            warn.append(
                "{} {}: `- 決定:` が無い。見出しの要約で埋めた"
                .format(d["タスク"], d["日付"])
            )
            d["決定"] = d["要約"]
        d["対象"] = flatten(d["対象"])
        d["決定"] = flatten(d["決定"])
    return decisions


def collect(root: str, warn: list) -> list:
    decisions = []
    for task_id, path in task_files(root):
        text = read_text(path)
        if not text:
            continue
        decisions.extend(parse_decisions(task_id, decision_log(text), warn))
    return decisions


# --------------------------------------------------------------------------
# 失効の解決
# --------------------------------------------------------------------------

def apply_supersede(decisions: list, warn: list) -> None:
    """`上書き対象` を読んで、古い側の状態を「置き換え済み」に変える。

    **書き換えるのは索引の行だけで、タスク別ファイルには一切触らない。**
    """
    by_key = {(d["タスク"], d["日付"]): d for d in decisions}
    for d in decisions:
        d["状態"] = ACTIVE
    for d in decisions:
        ref = d.get("上書き対象", "")
        if not ref:
            continue
        hits = SUPERSEDE_REF.findall(ref)
        if not hits:
            warn.append(
                "{} {}: `- 上書き対象:` の書き方が読めない（`T-003 2026-05-10` の形で書く。"
                "複数あれば ` / ` で区切る）: {}".format(d["タスク"], d["日付"], flatten(ref))
            )
            continue
        for task_id, date in hits:
            target = by_key.get((task_id, date))
            if target is None:
                warn.append(
                    "{} {}: `- 上書き対象: {} {}` が指す決定が見つからない"
                    "（タスクIDと日付の両方が一致する決定を指すこと。1つのタスクが"
                    "決定を複数持ちうるため日付まで要る）"
                    .format(d["タスク"], d["日付"], task_id, date)
                )
                continue
            target["状態"] = SUPERSEDED.format(d["タスク"])


# --------------------------------------------------------------------------
# 索引を組み立てる
# --------------------------------------------------------------------------

def group_key(target: str) -> tuple:
    """対象の並び順。「未分類」の束は末尾へ置く。書き忘れであって領域名ではない。"""
    return (1, "") if target == UNCLASSIFIED else (0, target)


def sort_key(row: dict) -> tuple:
    """対象の名前順 → その中は日付の新しい順 → タスク番号の数の順。"""
    m = re.match(r"T-(\d+)$", row["タスク"])
    num = (0, int(m.group(1))) if m else (1, 0)
    return (group_key(row["対象"]),
            tuple(-int(p) for p in row["日付"].split("-")), num)


def anchor(target: str) -> str:
    """見出しへのリンク先。空白をハイフンへ替えるだけの素朴な作り。

    日本語の見出しはそのまま使えるので、ここで凝る必要が無い。
    """
    return "#" + target.strip().lower().replace(" ", "-")


def cell(value: str) -> str:
    """表の1マスに収める。縦棒は列の区切りと読まれるので逃がす。"""
    return value.replace("|", "\\|")


def group_by_target(rows: list) -> list:
    """対象ごとに束ねる。並べ替え済みの列を前提に、順に見ていくだけ。"""
    groups: list = []
    for row in rows:
        if not groups or groups[-1][0] != row["対象"]:
            groups.append((row["対象"], []))
        groups[-1][1].append(row)
    return groups


def render(decisions: list) -> str:
    groups = group_by_target(sorted(decisions, key=sort_key))
    out = [TITLE, "", GENERATED_NOTE, ""]

    # 冒頭の対象一覧。**ここだけ読めば、どの領域に決定があるかが分かる。**
    # 語をまだ知らない側が辿り着くための入口であり、索引の要点そのものである。
    out.append("## 対象の一覧")
    out.append("")
    for target, items in groups:
        live = sum(1 for r in items if r["状態"] == ACTIVE)
        note = "{}件".format(len(items))
        if live != len(items):
            note += "（うち有効 {}件）".format(live)
        out.append("- [{}]({}) — {}".format(cell(target), anchor(target), note))
    out.append("")

    for target, items in groups:
        out.append("## " + target)
        out.append("")
        out.append("| " + " | ".join(COLUMNS) + " |")
        out.append("| " + " | ".join("---" for _ in COLUMNS) + " |")
        for r in items:
            out.append("| " + " | ".join(cell(r.get(k, "")) for k in COLUMNS) + " |")
        out.append("")

    return "\n".join(out).rstrip("\n") + "\n"


def read_index(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def write_index(path: str, text: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def print_warnings(warn: list, quiet: bool) -> None:
    """書式の警告を出す。--hook のときは件数だけ（セッション開始時に長く喋らない）。"""
    if not warn:
        return
    if quiet:
        print("[org-decisions] 決定ログの書式に警告 {} 件。"
              "`org-decisions.py` を --hook 無しで実行すると中身が出る。".format(len(warn)))
        return
    print("")
    print("[org-decisions] 決定ログの書式に警告 {} 件:".format(len(warn)))
    for w in warn:
        print("  - {}".format(w))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="決定ログの索引を、タスク別ファイルから生成する。",
    )
    ap.add_argument("--root", default=".", help="対象リポジトリのルート（既定: カレントディレクトリ）")
    ap.add_argument("--check", action="store_true",
                    help="書き込まず、索引が古くなっていないかだけ見る")
    ap.add_argument("--hook", action="store_true",
                    help="フックから呼ぶとき。何が起きても 0 で終わり、出力を抑える")
    args = ap.parse_args(argv)

    quiet = args.hook
    path = os.path.join(args.root, INDEX)

    files = task_files(args.root)
    if not files:
        if not quiet:
            print("[org-decisions] タスク別ファイル（{}/T-XXX.md）が1本も無い。何もしない。"
                  .format(TASKS_DIR.replace(os.sep, "/")))
        return 0

    warn: list = []
    decisions = collect(args.root, warn)

    if not decisions:
        current = read_index(path)
        if current is not None and not args.check:
            try:
                os.remove(path)
                if not quiet:
                    print("[org-decisions] 決定が1件も無くなったので、索引を消した。")
            except OSError:
                pass
        elif not quiet:
            print("[org-decisions] 決定ログに決定が1件も無い（タスク別ファイル {} 本）。索引は作らない。"
                  .format(len(files)))
        print_warnings(warn, quiet)
        return 0

    apply_supersede(decisions, warn)
    wanted = render(decisions)
    current = read_index(path)

    if args.check:
        if current != wanted:
            print("[org-decisions] 決定ログ索引（{}）が古い。".format(INDEX.replace(os.sep, "/")))
            print("[org-decisions] `org-decisions.py` を --check なしで実行すると作り直せる。")
            return 1
        print("[org-decisions] 決定ログ索引は最新（決定 {} 件）。".format(len(decisions)))
        return 0

    if current != wanted:
        write_index(path, wanted)
        verb = "作り直した"
    else:
        verb = "最新のまま"

    if quiet:
        print("[org-decisions] 決定 {} 件を索引へ（{}）。".format(len(decisions), verb))
        print_warnings(warn, quiet)
        return 0

    print("[org-decisions] {} を{}（決定 {} 件 / タスク別ファイル {} 本）。"
          .format(INDEX.replace(os.sep, "/"), verb, len(decisions), len(files)))
    superseded = sum(1 for d in decisions if d["状態"] != ACTIVE)
    if superseded:
        print("[org-decisions] うち {} 件は、後の決定に置き換えられている。".format(superseded))
    print_warnings(warn, quiet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
