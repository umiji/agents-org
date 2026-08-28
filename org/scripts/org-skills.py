#!/usr/bin/env python3
"""この実行環境から呼べるスキルを集めて一覧する。

スキルとは、Claude Code が「呼ばれたときだけ読み込む手順書」である。1本が
1つのディレクトリで、その中の `SKILL.md` の冒頭に名前と説明が書いてある。

このスクリプトは、組織を開発対象リポジトリへ導入するときの初期シーケンス
（スキル `org-init`）の第1段で使う。**何が在るかを集めるだけで、どれを使うかは
決めない。** 判断はオーケストレーターが行う。集めるだけの作業を LLM にやらせると、
トークンを消費したうえに数え落としが起きるため、ここで機械にやらせる。

Python 3.8 以降。標準ライブラリのみ。追加インストールは要らない。

使い方:
    python3 org-skills.py                  呼べるスキルを一覧する（名前・説明・見込みトークン量）
    python3 org-skills.py --names          名前だけ出す（読ませるトークンを節約したいとき）
    python3 org-skills.py --search 語      名前か説明に語を含むものだけ出す
    python3 org-skills.py --full           説明を切り詰めずに全文出す
    python3 org-skills.py --json           機械可読の形で出す
    python3 org-skills.py --catalog        まだ導入していないプラグインの目録を出す
    python3 org-skills.py --root path      対象リポジトリを指定する（既定: カレント）

`python3` という名前のコマンドが無い環境（Windows の標準的な導入ではこれが普通）
では `python` に読み替える。

**このスクリプトは何も書き換えない。読むだけである。**

--------------------------------------------------------------------------
スキルが置かれている場所は3か所ある
--------------------------------------------------------------------------

  プラグイン    導入済みプラグインの中。`~/.claude/plugins/` の下に実体がある。
                呼ぶときの名前は `プラグイン名:スキル名`（例 `ecc:security-review`）
  利用者        `~/.claude/skills/` の下。どのリポジトリからも見える
  リポジトリ    対象リポジトリの `.claude/skills/` の下。リポジトリと一緒に運べる

**プラグインには、特定のリポジトリでだけ有効にしてあるものがある。** その場合は
対象リポジトリ（`--root`）が一致するときだけ数える。一致しないものを一覧へ出すと、
「在るはずなのに呼べない」という取り違えが起きる。

**プラグインが持ち込む「コマンド」（`/名前` で人間が打つ入口）は数えない。**
スキルと似た見え方をするが別物であり、`SKILL.md` を持たない。担当エージェントへ
割り当てる対象はスキルのほうである。

--------------------------------------------------------------------------
`--catalog` は何を出すか
--------------------------------------------------------------------------

**まだ導入していない**プラグインの目録である。Claude Code には配布元
（マーケットプレイス）を登録する仕組みがあり、登録すると、その配布元が持つ
プラグインの目録がローカルのファイルへ落ちてくる。`--catalog` はそれを読む。

**ネット接続は要らない。** また、Claude Code のコマンドにはスキルやプラグインを
検索する機能が無いため、この目録がその代わりを果たす。

**導入そのものはこのスクリプトでは行わない。** プラグインはスキルだけでなく
エージェント定義やフック（特定のタイミングで必ずコマンドを実行させる仕組み）も
持ち込むため、PO（プロジェクトオーナー＝人間の依頼主）の承認が要る。

終了コード:
    0  一覧を出した（1本も無い場合を含む）
    2  設定ディレクトリが読めない
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# 見込みトークン量の概算に使う係数。英語の散文でおよそ4文字＝1トークンになる。
# 正確な数え方は実装依存で、ここで再現する意味は無い。**桁が分かればよい。**
CHARS_PER_TOKEN = 4

# 一覧に出すときの説明の長さ。これを超えたら切り詰める（`--full` で全文になる）。
DESCRIPTION_WIDTH = 110

# 出所の並び順。上から順に出す。
ORIGIN_ORDER = ["プラグイン", "利用者", "リポジトリ"]


class EnvError(Exception):
    """設定ディレクトリが読めない。終了コード 2 になる。"""


# --------------------------------------------------------------------------
# 置き場所を突き止める
# --------------------------------------------------------------------------

def config_dir(override: str = None) -> str:
    """Claude Code の設定ディレクトリ。環境変数で移せるので、それを優先する。"""
    if override:
        return override
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude"
    )


def same_path(a: str, b: str) -> bool:
    """2つのパスが同じ場所を指すか。Windows は大文字小文字を区別しない。"""
    def norm(p):
        return os.path.normcase(os.path.normpath(os.path.abspath(p)))
    return norm(a) == norm(b)


def read_json(path: str):
    """JSON を読む。無ければ None。壊れていても None（環境の問題で止めない）。"""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------
# SKILL.md の冒頭（フロントマター）を読む
# --------------------------------------------------------------------------

def parse_front_matter(text: str) -> dict:
    """`---` で挟まれた冒頭の `キー: 値` を読む。

    値が次の行へ折り返されている書き方（行頭に空白がある続き行）に対応する。
    実際の SKILL.md には説明を2行に折り返したものがあり、そこで切ると説明が
    途中で切れる。

    YAML の完全な解析はしない。ここで要るのは `name` と `description` だけで、
    そのために外部ライブラリを足すのは配布の条件（追加インストールを要求しない）
    に反する。
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}

    fields = {}
    key = None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line[:1] in (" ", "\t"):
            # 続き行。直前のキーの値へつなぐ。
            if key:
                fields[key] = (fields[key] + " " + line.strip()).strip()
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        # `description: >-` のように、値を次の行から書き始める記法（YAML の
        # ブロック記法）がある。記号そのものは値ではないので捨てる。捨てないと
        # 説明文の頭に `>-` が付いたまま一覧に出る。
        if value and value[0] in ("|", ">") and value[1:].strip(" 0123456789+-") == "":
            value = ""
        fields[key] = unquote(value)
    return fields


def unquote(value: str) -> str:
    """前後を囲っている引用符を外す。"""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def read_skill(skill_dir: str, origin: str, group: str, prefix: str = ""):
    """スキル1本を読む。`SKILL.md` が無ければ None（スキルではない）。"""
    path = os.path.join(skill_dir, "SKILL.md")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None

    fields = parse_front_matter(text)
    # フロントマターの `name` より、ディレクトリ名のほうが呼び出しに使われる。
    # 食い違っている SKILL.md が実在するため、ディレクトリ名を採る。
    bare = os.path.basename(os.path.normpath(skill_dir))
    return {
        "name": prefix + bare,
        "origin": origin,
        "group": group,
        "description": fields.get("description", ""),
        "chars": len(text),
        "tokens": round(len(text) / CHARS_PER_TOKEN),
        "path": path,
    }


def scan_skill_root(root: str, origin: str, group: str, prefix: str = "") -> list:
    """スキルを束ねたディレクトリを1階層だけ見る。

    1階層に留めるのは、`~/.claude/skills/learned/` のように**スキルを入れる
    ための空の入れ物**が実在し、深く潜ると分類名をスキル名と取り違えるため。
    """
    if not os.path.isdir(root):
        return []
    found = []
    for entry in sorted(os.listdir(root)):
        skill = read_skill(os.path.join(root, entry), origin, group, prefix)
        if skill:
            found.append(skill)
    return found


# --------------------------------------------------------------------------
# 導入済みプラグイン
# --------------------------------------------------------------------------

def plugin_install_paths(home: str, root: str) -> list:
    """導入済みプラグインの実体の置き場を、対象リポジトリから見える分だけ返す。

    プラグインは利用者全体へ導入されることも、特定のリポジトリでだけ有効に
    することもできる。後者は、対象リポジトリが一致するときだけ数える。
    """
    installed = read_json(os.path.join(home, "plugins", "installed_plugins.json"))
    if not isinstance(installed, dict):
        return []
    plugins = installed.get("plugins")
    if not isinstance(plugins, dict):
        return []

    entries = []
    for full_name, records in sorted(plugins.items()):
        if not isinstance(records, list):
            continue
        # `プラグイン名@配布元名` の形。呼び出しに使うのは前半だけ。
        plugin_name = full_name.split("@", 1)[0]
        marketplace = full_name.split("@", 1)[1] if "@" in full_name else ""
        for record in records:
            if not isinstance(record, dict):
                continue
            if record.get("scope") == "project":
                project = record.get("projectPath")
                if not project or not same_path(project, root):
                    continue
            path = resolve_install_path(home, record, marketplace, plugin_name)
            if path and not any(same_path(path, e["path"]) for e in entries):
                entries.append({
                    "plugin": plugin_name,
                    "full_name": full_name,
                    "path": path,
                })
    return entries


def resolve_install_path(home: str, record: dict, marketplace: str, plugin: str):
    """プラグインの実体がどこに展開されているかを決める。

    記録されている絶対パスをそのまま信じない。設定ディレクトリごと別の場所へ
    写した環境（`--claude-home` を指定した検証を含む）では、記録された絶対パスが
    もう存在しないためである。**設定ディレクトリからの組み立てを先に試す。**
    """
    version = record.get("version")
    if marketplace and plugin and version:
        built = os.path.join(home, "plugins", "cache", marketplace, plugin, str(version))
        if os.path.isdir(built):
            return built
    recorded = record.get("installPath")
    if recorded and os.path.isdir(recorded):
        return recorded
    return None


def plugin_skill_dirs(install_path: str) -> list:
    """1つのプラグインの中で、スキルが置かれているディレクトリを返す。

    プラグインは自分の定義ファイル（`.claude-plugin/plugin.json`）で置き場を
    宣言できる。宣言が無ければ `skills/` を見る。
    """
    manifest = read_json(os.path.join(install_path, ".claude-plugin", "plugin.json"))
    declared = manifest.get("skills") if isinstance(manifest, dict) else None
    if not isinstance(declared, list) or not declared:
        declared = ["./skills/"]

    dirs = []
    for entry in declared:
        if not isinstance(entry, str):
            continue
        path = os.path.normpath(os.path.join(install_path, entry))
        if os.path.isdir(path) and not any(same_path(path, d) for d in dirs):
            dirs.append(path)
    return dirs


def collect_plugin_skills(home: str, root: str) -> list:
    found = []
    for entry in plugin_install_paths(home, root):
        group = "プラグイン {}".format(entry["full_name"])
        for skills_dir in plugin_skill_dirs(entry["path"]):
            # 宣言された場所そのものが1本のスキルであることもある。
            single = read_skill(skills_dir, "プラグイン", group, entry["plugin"] + ":")
            if single:
                found.append(single)
                continue
            found.extend(scan_skill_root(
                skills_dir, "プラグイン", group, entry["plugin"] + ":"
            ))
    return found


# --------------------------------------------------------------------------
# 収集の入口
# --------------------------------------------------------------------------

def collect(home: str, root: str) -> list:
    """呼べるスキルを3か所から集める。"""
    if not os.path.isdir(home):
        raise EnvError("設定ディレクトリが見つからない: {}".format(home))

    found = []
    found.extend(collect_plugin_skills(home, root))
    found.extend(scan_skill_root(
        os.path.join(home, "skills"), "利用者", "利用者共通（~/.claude/skills/）"
    ))
    found.extend(scan_skill_root(
        os.path.join(root, ".claude", "skills"), "リポジトリ",
        "このリポジトリ（.claude/skills/）"
    ))

    # 同じ名前が複数の場所に在ることがある。先に見つかったほうを残す。
    unique = []
    seen = set()
    for skill in found:
        if skill["name"] in seen:
            continue
        seen.add(skill["name"])
        unique.append(skill)
    return unique


def sort_key(skill: dict):
    try:
        origin_rank = ORIGIN_ORDER.index(skill["origin"])
    except ValueError:
        origin_rank = len(ORIGIN_ORDER)
    return (origin_rank, skill["group"], skill["name"])


def filter_skills(skills: list, word: str) -> list:
    """名前か説明に語を含むものだけ残す。大文字小文字は区別しない。"""
    if not word:
        return skills
    needle = word.lower()
    return [
        s for s in skills
        if needle in s["name"].lower() or needle in s["description"].lower()
    ]


# --------------------------------------------------------------------------
# まだ導入していないプラグインの目録
# --------------------------------------------------------------------------

def marketplace_dir(home: str, name: str, record: dict):
    """配布元の目録がどこに落ちているかを決める。組み立てを先に試す。"""
    built = os.path.join(home, "plugins", "marketplaces", name)
    if os.path.isdir(built):
        return built
    recorded = record.get("installLocation") if isinstance(record, dict) else None
    if recorded and os.path.isdir(recorded):
        return recorded
    return None


def collect_catalog(home: str, root: str) -> list:
    """登録済み配布元が持つプラグインを、名前と説明つきで集める。"""
    if not os.path.isdir(home):
        raise EnvError("設定ディレクトリが見つからない: {}".format(home))

    known = read_json(os.path.join(home, "plugins", "known_marketplaces.json"))
    if not isinstance(known, dict):
        return []

    installed = {e["full_name"] for e in plugin_install_paths(home, root)}

    catalog = []
    for market_name, record in sorted(known.items()):
        directory = marketplace_dir(home, market_name, record)
        if not directory:
            continue
        manifest = read_json(
            os.path.join(directory, ".claude-plugin", "marketplace.json")
        )
        plugins = manifest.get("plugins") if isinstance(manifest, dict) else None
        if not isinstance(plugins, list):
            continue
        for plugin in plugins:
            if not isinstance(plugin, dict) or not plugin.get("name"):
                continue
            full_name = "{}@{}".format(plugin["name"], market_name)
            catalog.append({
                "name": plugin["name"],
                "full_name": full_name,
                "marketplace": market_name,
                "category": plugin.get("category", ""),
                "description": plugin.get("description", ""),
                "installed": full_name in installed,
            })
    return catalog


# --------------------------------------------------------------------------
# 出力
# --------------------------------------------------------------------------

def make_output_safe() -> None:
    """出力の文字コードで表現できない文字が来ても、落ちずに続けるようにする。

    一覧に出す説明文は**他人が書いたもの**なので、この実行環境の文字コードで
    表現できない文字（長いダッシュ、矢印、絵文字）が普通に混ざる。Windows で
    出力を別のコマンドへ渡すと、既定の文字コード（cp932）がそれを表現できず、
    **一覧の途中で異常終了する。** 表現できない文字は `?` へ置き換えて続ける。
    """
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, OSError, ValueError):
        # 出力先を差し替えている場合（検証など）。そのまま続ける。
        pass


def shorten(text: str, width: int) -> str:
    text = " ".join(text.split())
    if len(text) <= width:
        return text
    return text[: width - 1].rstrip() + "…"


def thousands(number: int) -> str:
    return "{:,}".format(number)


def print_skills(skills: list, names_only: bool, full: bool, out) -> None:
    if not skills:
        out.write("[org-skills] 呼べるスキルが1本も見つからない。\n")
        out.write("             プラグインを導入していないか、設定ディレクトリの"
                  "場所が違う可能性がある。\n")
        return

    by_origin = {}
    for skill in skills:
        by_origin[skill["origin"]] = by_origin.get(skill["origin"], 0) + 1
    breakdown = " / ".join(
        "{} {}".format(origin, by_origin[origin])
        for origin in ORIGIN_ORDER if origin in by_origin
    )
    out.write("[org-skills] 呼べるスキル {} 本（{}）\n".format(
        len(skills), breakdown))

    if names_only:
        out.write("\n")
        for skill in skills:
            out.write("{}\n".format(skill["name"]))
        cost = round(sum(len(s["name"]) + 1 for s in skills) / CHARS_PER_TOKEN)
        out.write("\n[org-skills] この一覧じたいの見込み量: 約 {} トークン\n".format(
            thousands(cost)))
        return

    width = max((len(s["name"]) for s in skills), default=0)
    group = None
    for skill in skills:
        if skill["group"] != group:
            group = skill["group"]
            count = sum(1 for s in skills if s["group"] == group)
            out.write("\n## {}（{}本）\n\n".format(group, count))
        description = skill["description"]
        if not full:
            description = shorten(description, DESCRIPTION_WIDTH)
        out.write("  {name:<{width}}  約{tokens:>6} トークン  {desc}\n".format(
            name=skill["name"], width=width,
            tokens=thousands(skill["tokens"]), desc=description or "（説明なし）"))

    body = sum(s["tokens"] for s in skills)
    out.write("\n[org-skills] 本文をすべて読み込んだ場合の見込み量: "
              "約 {} トークン\n".format(thousands(body)))
    out.write("[org-skills] **本文はスキルを呼んだときだけ読み込まれる。** "
              "全部を常時抱えるわけではない。\n")


def print_catalog(catalog: list, full: bool, out) -> None:
    if not catalog:
        out.write("[org-skills] 登録済みの配布元に目録が見つからない。\n")
        return

    remaining = [p for p in catalog if not p["installed"]]
    out.write("[org-skills] 登録済み配布元のプラグイン {} 件"
              "（うち未導入 {} 件）\n".format(len(catalog), len(remaining)))
    out.write("[org-skills] **導入（`claude plugin install`）は PO の承認後に"
              "限る。** プラグインはスキルだけでなく\n")
    out.write("             エージェント定義やフック（特定のタイミングで必ず"
              "コマンドを実行させる仕組み）も持ち込む。\n")

    width = max((len(p["full_name"]) for p in catalog), default=0)
    market = None
    for plugin in catalog:
        if plugin["marketplace"] != market:
            market = plugin["marketplace"]
            count = sum(1 for p in catalog if p["marketplace"] == market)
            out.write("\n## 配布元 {}（{}件）\n\n".format(market, count))
        description = plugin["description"]
        if not full:
            description = shorten(description, DESCRIPTION_WIDTH)
        out.write("  {mark} {name:<{width}}  {desc}\n".format(
            mark="導入済" if plugin["installed"] else "　　　",
            name=plugin["full_name"], width=width,
            desc=description or "（説明なし）"))


# --------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="この実行環境から呼べるスキルを集めて一覧する（読むだけ）")
    ap.add_argument("--root", default=".",
                    help="対象リポジトリのルート（既定: カレントディレクトリ）")
    ap.add_argument("--claude-home",
                    help="設定ディレクトリの場所（既定: ~/.claude）")
    ap.add_argument("--names", action="store_true",
                    help="名前だけ出す（読ませるトークンを節約したいとき）")
    ap.add_argument("--search", metavar="語",
                    help="名前か説明に語を含むものだけ出す")
    ap.add_argument("--full", action="store_true",
                    help="説明を切り詰めずに全文出す")
    ap.add_argument("--json", action="store_true", dest="as_json",
                    help="機械可読の形で出す")
    ap.add_argument("--catalog", action="store_true",
                    help="まだ導入していないプラグインの目録を出す")
    args = ap.parse_args(argv)

    make_output_safe()
    root = os.path.abspath(args.root)
    home = config_dir(args.claude_home)

    try:
        if args.catalog:
            items = collect_catalog(home, root)
            items = filter_skills(items, args.search)
            items.sort(key=lambda p: (p["marketplace"], p["name"]))
        else:
            items = collect(home, root)
            items = filter_skills(items, args.search)
            items.sort(key=sort_key)
    except EnvError as exc:
        print("[org-skills] {}".format(exc))
        return 2

    if args.as_json:
        json.dump(items, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0

    if args.catalog:
        print_catalog(items, args.full, sys.stdout)
    else:
        print_skills(items, args.names, args.full, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
