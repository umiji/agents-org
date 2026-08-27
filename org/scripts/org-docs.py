#!/usr/bin/env python3
"""層3ドキュメントの人間用ビューを、エージェント用マスタから生成する。

AI開発組織が開発対象リポジトリに書くドキュメント（層3）は2階建てである。

  エージェント用マスタ  docs/features/ docs/guides/ docs/api/ の Markdown
                        機能・目的・シーン単位に細かく分割してある。**正典**
  人間用ビュー          docs/handbook.md（通し読み用）と README.md の索引
                        マスタから**生成する**。手で書かない

このスクリプトは後者を作る。結合と目次づくりは判断を伴わない機械的処理なので、
LLM にやらせない——トークンを消費したうえに、通すたびに内容が書き換わるため。

Python 3.8 以降。標準ライブラリのみ。追加インストールは要らない。

使い方:
    python3 org-docs.py                     人間用ビューを作り直す
    python3 org-docs.py --check             作り直さず、古くなっていないかだけ見る
    python3 org-docs.py --root path/to/repo 対象リポジトリを指定する（既定: カレント）

`python3` という名前のコマンドが無い環境（Windows の標準的な導入ではこれが普通）
では `python` に読み替える。

書き込む先は2つだけである。

  docs/handbook.md   丸ごと作り直す。冒頭に「手で書かない」と明記した生成物
  README.md          **目印で挟んだ区間だけ**を差し替える。目印が無ければ触らない

README を丸ごと生成しない理由: README には題名・導入・ライセンス・バッジなど、
マスタから導けない手書きの内容が普通に入っている。丸ごと作り直すと、それらを
黙って消してしまう。索引の置き場所は書き手が目印で指定する。

    <!-- org:docs:begin -->
    <!-- org:docs:end -->

終了コード:
    0  生成した（または、マスタが無いので何もしなかった）
    1  --check で、生成物が古い・欠けていると分かった
    2  マスタが読めない
"""

from __future__ import annotations

import argparse
import os
import re
import sys

# --- マスタの置き場（decisions/06-documentation-agent.md 確定事項 A と一致させること）---

# 並べる順序でもある。上から順に handbook へ入る。
CATEGORIES = [
    ("features", "機能"),
    ("guides", "目的・シーン別ガイド"),
    ("api", "API リファレンス"),
]

HANDBOOK = os.path.join("docs", "handbook.md")
README = "README.md"

BEGIN = "<!-- org:docs:begin -->"
END = "<!-- org:docs:end -->"

GENERATED_NOTE = (
    "> **このファイルは生成物である。手で書かない。**\n"
    "> 直すのは `docs/features/` `docs/guides/` `docs/api/` の下にあるマスタのほうで、\n"
    "> このファイルは `org-docs.py`（人間用ビューの生成スクリプト）が作り直す。\n"
)

FENCE = re.compile(r"^\s{0,3}(```+|~~~+)")
HEADING = re.compile(r"^(#{1,6})(\s+)(.*)$")
LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")


class MasterError(Exception):
    """マスタが読めない。"""


# --------------------------------------------------------------------------
# マスタを集める
# --------------------------------------------------------------------------

class Master:
    """マスタ1本ぶん。"""

    def __init__(self, category, label, relpath, title, summary, lines):
        self.category = category      # "features" など、置き場のディレクトリ名
        self.label = label            # 「機能」など、人間に見せる分類名
        self.relpath = relpath        # リポジトリのルートからの相対パス（区切りは "/"）
        self.title = title            # 題（先頭の見出し。無ければファイル名）
        self.summary = summary        # 最初の本文1行。索引に載せる
        self.lines = lines            # 本文（題の見出し行を除いた残り）
        self.anchor = ""              # handbook 内での行き先。あとで決める


def collect(root):
    """マスタを、決めた順序で集めて返す。"""
    found = []
    for category, label in CATEGORIES:
        directory = os.path.join(root, "docs", category)
        if not os.path.isdir(directory):
            continue
        try:
            names = sorted(n for n in os.listdir(directory) if n.endswith(".md"))
        except OSError as exc:
            raise MasterError("{}: {}".format(directory, exc))
        for name in names:
            path = os.path.join(directory, name)
            if not os.path.isfile(path):
                continue
            found.append(read_master(path, category, label, name))
    return found


def read_master(path, category, label, name):
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise MasterError("{}: {}".format(path, exc))

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    title, body = split_title(lines, fallback=name[:-len(".md")])
    return Master(
        category=category,
        label=label,
        relpath="docs/{}/{}".format(category, name),
        title=title,
        summary=first_sentence(body),
        lines=body,
    )


def split_title(lines, fallback):
    """先頭の見出しを題として取り出し、残りの本文と一緒に返す。

    見出しが無いマスタは書式違反だが、ここで止めても直せるのは人間なので、
    ファイル名を題に使って生成を続ける。どのファイルかは索引のリンクで分かる。
    """
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        m = HEADING.match(line)
        if m and len(m.group(1)) == 1:
            return m.group(3).strip(), lines[i + 1:]
        break
    return fallback, lines


def first_sentence(lines):
    """索引に載せる1行。本文の最初の「ふつうの段落」の1行目を使う。"""
    in_fence = False
    for line in lines:
        stripped = line.strip()
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence or not stripped:
            continue
        if stripped[0] in "#>|-*+" or stripped.startswith("<!--"):
            continue
        return stripped
    return ""


# --------------------------------------------------------------------------
# 行き先（アンカー）を決める
# --------------------------------------------------------------------------

def slugify(title):
    """見出しから、Markdown 表示側が作るのと同じ形の行き先を組み立てる。

    小文字にし、記号を落とし、空白をハイフンにする。日本語はそのまま残す
    （GitHub の表示もそうしている）。
    """
    s = title.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    s = re.sub(r"\s+", "-", s)
    return s.strip("-")


def assign_anchors(masters):
    """題が重なったときに行き先が衝突しないよう、通し番号を足す。"""
    used = {}
    for m in masters:
        base = slugify(m.title) or slugify(os.path.basename(m.relpath))
        n = used.get(base, 0)
        used[base] = n + 1
        m.anchor = base if n == 0 else "{}-{}".format(base, n)


# --------------------------------------------------------------------------
# 本文を結合できる形に直す
# --------------------------------------------------------------------------

def shift_headings(lines):
    """見出しを1段下げる。コードブロックの中は触らない。

    handbook では各マスタの題が2段目の見出しになるので、中の見出しも
    そのぶん下げないと、次のマスタの題より上に立ってしまう。
    """
    out = []
    in_fence = False
    fence_mark = ""
    for line in lines:
        m = FENCE.match(line)
        if m:
            mark = m.group(1)[0]
            if not in_fence:
                in_fence, fence_mark = True, mark
            elif mark == fence_mark:
                in_fence = False
            out.append(line)
            continue
        h = HEADING.match(line)
        if h and not in_fence and len(h.group(1)) < 6:
            out.append("#" + h.group(1) + h.group(2) + h.group(3))
        else:
            out.append(line)
    return out


def rewrite_links(lines, master, by_path):
    """マスタ同士のリンクを、結合後の文書内の行き先に書き換える。

    書き換えないと、1枚に結合した文書の中から、もう存在しない相対パスへ
    飛ばそうとするリンクが残る。マスタでないもの（外部URL、他のファイル）は
    そのまま残す。
    """
    base = os.path.dirname(master.relpath)

    def replace(m):
        text, target = m.group(1), m.group(2)
        path, _, fragment = target.partition("#")
        if not path or "://" in path or path.startswith(("/", "#", "mailto:")):
            return m.group(0)
        resolved = os.path.normpath(os.path.join(base, path)).replace(os.sep, "/")
        hit = by_path.get(resolved)
        if hit is None:
            return m.group(0)
        # 見出しまで指しているリンクも、行き先はその節の先頭でよい。
        # 結合すると節の行き先が変わりうるため、確実に存在するほうへ寄せる。
        del fragment
        return "[{}](#{})".format(text, hit.anchor)

    out = []
    in_fence = False
    for line in lines:
        if FENCE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        out.append(line if in_fence else LINK.sub(replace, line))
    return out


# --------------------------------------------------------------------------
# 生成
# --------------------------------------------------------------------------

def build_handbook(masters):
    by_path = {m.relpath: m for m in masters}
    parts = ["# ハンドブック", "", GENERATED_NOTE.rstrip("\n"), "", "## 目次", ""]

    current = None
    for m in masters:
        if m.category != current:
            if current is not None:
                parts.append("")
            current = m.category
            parts.append("**{}**".format(m.label))
            parts.append("")
        parts.append("- [{}](#{})".format(m.title, m.anchor))
    parts.append("")

    for m in masters:
        parts.append("---")
        parts.append("")
        parts.append("## {}".format(m.title))
        parts.append("")
        parts.append("*出典: `{}`*".format(m.relpath))
        parts.append("")
        body = rewrite_links(shift_headings(m.lines), m, by_path)
        parts.extend(trim(body))
        parts.append("")

    return "\n".join(trim(parts)) + "\n"


def build_index(masters):
    """README に差し込む索引。"""
    parts = []
    current = None
    for m in masters:
        if m.category != current:
            if current is not None:
                parts.append("")
            current = m.category
            parts.append("### {}".format(m.label))
            parts.append("")
        line = "- [{}]({})".format(m.title, m.relpath)
        if m.summary:
            line += " — {}".format(m.summary)
        parts.append(line)
    return "\n".join(parts)


def trim(lines):
    """前後の空行を落とす。結合のたびに空行が増えるのを防ぐ。"""
    out = list(lines)
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    return out


def splice_readme(text, index):
    """README の目印で挟まれた区間だけを差し替える。

    目印が見つからなければ None を返す。**その場合 README は触らない。**
    """
    start = text.find(BEGIN)
    end = text.find(END)
    if start < 0 or end < 0 or end < start:
        return None
    head = text[:start + len(BEGIN)]
    tail = text[end:]
    return "{}\n{}\n{}".format(head, index, tail)


# --------------------------------------------------------------------------
# 入出力
# --------------------------------------------------------------------------

def read_if_exists(path):
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().replace("\r\n", "\n").replace("\r", "\n")
    except (OSError, UnicodeDecodeError):
        return None


def write(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="層3ドキュメントの人間用ビューを、エージェント用マスタから生成する。",
    )
    ap.add_argument("--root", default=".", help="対象リポジトリのルート（既定: カレントディレクトリ）")
    ap.add_argument("--check", action="store_true",
                    help="書き込まず、生成物が古くなっていないかだけ見る")
    args = ap.parse_args(argv)

    root = args.root

    try:
        masters = collect(root)
    except MasterError as exc:
        print("[org-docs] マスタが読めない: {}".format(exc))
        return 2

    if not masters:
        print("[org-docs] エージェント用マスタ（docs/features/ docs/guides/ docs/api/）が"
              "1本も無い。何もしない。")
        return 0

    assign_anchors(masters)
    handbook = build_handbook(masters)
    index = build_index(masters)

    handbook_path = os.path.join(root, HANDBOOK)
    readme_path = os.path.join(root, README)

    current_handbook = read_if_exists(handbook_path)
    current_readme = read_if_exists(readme_path)
    wanted_readme = None if current_readme is None else splice_readme(current_readme, index)

    if args.check:
        stale = []
        if current_handbook != handbook:
            stale.append(HANDBOOK)
        if wanted_readme is not None and wanted_readme != current_readme:
            stale.append("{} の索引".format(README))
        if stale:
            print("[org-docs] 人間用ビューが古い: {}".format(" / ".join(stale)))
            print("[org-docs] `org-docs.py` を --check なしで実行すると作り直せる。")
            return 1
        print("[org-docs] 人間用ビューはマスタと一致している（マスタ {} 本）。".format(len(masters)))
        return 0

    if current_handbook != handbook:
        write(handbook_path, handbook)
        print("[org-docs] {} を作り直した（マスタ {} 本）。".format(HANDBOOK, len(masters)))
    else:
        print("[org-docs] {} は最新（マスタ {} 本）。".format(HANDBOOK, len(masters)))

    if current_readme is None:
        print("[org-docs] {} が無いので索引は差し込まない。".format(README))
    elif wanted_readme is None:
        print("[org-docs] {} に目印が無いので触らない。索引を出したい場所へ"
              "次の2行を置くと、そこへ差し込む:".format(README))
        print("           {}".format(BEGIN))
        print("           {}".format(END))
    elif wanted_readme != current_readme:
        write(readme_path, wanted_readme)
        print("[org-docs] {} の索引を差し替えた（目印で挟んだ区間だけ）。".format(README))
    else:
        print("[org-docs] {} の索引は最新。".format(README))

    return 0


if __name__ == "__main__":
    sys.exit(main())
