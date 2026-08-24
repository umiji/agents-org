# org/ — AI開発組織の配布物

このディレクトリは、**開発対象リポジトリへ配置する組織の実行時ファイル**である。

本リポジトリ（`agents-org`）は組織の**定義**を持つ場所であり、組織が実際に動くのは開発対象リポジトリの側である。したがってここのファイルは、本リポジトリのルート `.claude/` へは置かない。理由は `docs/decisions/13-execution-form.md` の B を参照。

## 中身

| ファイル | 配置先（開発対象リポジトリ） | 内容 |
| --- | --- | --- |
| `CLAUDE.md` | リポジトリのルート | 層2の運営規約。組織の構成、標準フロー、共通規約、Git、直接通信 |
| `glossary.md` | `docs/` | **用語集。** 一般的な意味と違う語、範囲が曖昧になる語、このプロジェクトの語。最後の節はプロジェクト開始後にオーケストレーターが育てる |
| `rules/org-task-ledger.md` | `.claude/rules/` | タスク台帳の書き方（CSV索引 + タスク別Markdown） |
| `rules/org-escalation.md` | `.claude/rules/` | エスカレーションの4段階、共通停止条件、PO確認待ちキュー |
| `agents/org-design.md` | `.claude/agents/` | 設計エージェント |
| `agents/org-implementation.md` | `.claude/agents/` | 実装エージェント |
| `agents/org-review.md` | `.claude/agents/` | レビューエージェント（**編集系ツールを持たない**） |
| `agents/org-test.md` | `.claude/agents/` | テストエージェント |
| `agents/org-documentation.md` | `.claude/agents/` | ドキュメントエージェント |
| `skills/org-orchestrate/` | `.claude/skills/` | オーケストレーターの手順 |
| `skills/org-session-resume/` | `.claude/skills/` | セッション再開の手順 |
| `skills/org-first-run-check/` | `.claude/skills/` | **初回運用の点検手順。** 最初のタスクが1件完了したら使う |
| `scripts/org-check.py` | `.claude/scripts/` | **停滞検知と整合性検査。** Python 3.8 以降、標準ライブラリのみ |
| `settings.snippet.json` | — | セッション開始時に上のスクリプトを走らせる設定。**既存の設定へ追記する**（後述） |

**オーケストレーターのエージェント定義ファイルは無い。** オーケストレーターは**メインセッション自身**である。サブエージェントは `AskUserQuestion` を使えず、PO へ問い合わせできないため、サブエージェントであり得ない。

## 導入

```sh
# 開発対象リポジトリのルートで
mkdir -p .claude/agents .claude/rules .claude/skills
mkdir -p docs
cp    /path/to/agents-org/org/glossary.md        docs/
cp -r /path/to/agents-org/org/agents/*        .claude/agents/
cp -r /path/to/agents-org/org/rules/*         .claude/rules/
cp -r /path/to/agents-org/org/skills/*        .claude/skills/
mkdir -p .claude/scripts
cp    /path/to/agents-org/org/scripts/org-check.py  .claude/scripts/
```

`org/CLAUDE.md` は、開発対象リポジトリのルート `CLAUDE.md` へ**追記**する。既に `CLAUDE.md` があれば置き換えず、組織の節として足す。開発対象そのものの規約（技術スタック、ビルド手順、コーディング規約）は、その下へ書く。

### 既存拡張との共存

Everything Claude Code / Superpowers 等が導入済みでも共存する前提である。

- すべて `org-` プレフィックスを持つ。**既存の commands / skills / hooks を上書き・削除しない**
- hook を足す場合は、既存エントリを**置換せず追記**する
- 名前が衝突したら**組織側が譲る**（`org-` の後ろを変える）

### 停滞検知を自動で走らせる

`settings.snippet.json` の中身を、開発対象リポジトリの `.claude/settings.json` へ**追記**する。

**ファイルごと上書きしない。** 既存の設定（他の拡張が入れた hook を含む）を消してしまう。`hooks` の項目が既にあるなら、`SessionStart` の配列へ1件足す形にする。

これで、セッションが始まるたびに台帳が検査され、結果が Claude の文脈へ入る。

**`CLAUDE.md` へ書くだけでは駄目な理由**: `CLAUDE.md` は読まれる文脈であって、実行される設定ではない。「セッション開始時に停滞を確認せよ」と書いても、実行されない回が出る。必ず走らせたいものは hook にする。

### スクリプトの使い方

```sh
python3 .claude/scripts/org-check.py              # 停滞検知と整合性検査
python3 .claude/scripts/org-check.py --summary    # ゴール健全性の指標を集計
python3 .claude/scripts/org-check.py --statusline # 1行にまとめる（ステータス行向け）
python3 .claude/scripts/org-check.py --days 3     # 停滞と判定する日数を変える（既定 2）
```

終了コードは、検出なしで `0`、停滞や警告ありで `1`、台帳が読めないときに `2`。

**このスクリプトは判定するだけで、対処はしない。** 何をするかはオーケストレーターが決める。スクリプトが勝手に担当を変えると、なぜそうなったかが記録に残らないため。

## 既に別方式のタスク台帳を運用している場合

この組織の台帳は、`task-cycle`（索引の CSV + タスク別 Markdown）を土台にしている。**移行は差分だけで済むが、自動ではない。**

`python3 .claude/scripts/org-check.py` を実行すると、**直すべき箇所が機械的に列挙される。** それを潰していけばよい。

### 差分

| # | 直すもの | 内容 |
| --- | --- | --- |
| 1 | **索引の列を9列にする** | `状態` の後ろへ `優先度` と `担当` を挿入する。既存の行は `優先度=中` / `担当=未割当` で埋めてよい。**列が足りないと検査は終了コード 2 で止まる**（読み進めない） |
| 2 | **状態を10種へ揃える** | 下表のとおり。定義外の状態は警告になる |
| 3 | タスク別ファイルのメタデータへ4項目を足す | `優先度` / `担当` / `TDD適用可否` / `手戻り回数` |
| 4 | タスク別ファイルへ4セクションを足す | `## 判断してよい範囲` / `## 参照すべき成果物` / `## 成果物` / `## ブロッカー` |
| 5 | 依存の表記 | **直さなくてよい。** 英語表記もそのまま読める |

### 状態の対応（そのまま使えないものだけ）

| 旧 | 新 | 判断 |
| --- | --- | --- |
| `調査中` | `設計中` | 調べているのが「どう作るか」なら設計。既に手を動かしているなら `実装中` |
| `ローカル検証済み` | `テスト中` | 実装者の手元では動いた状態。第三者の検証がこれから |
| `実環境検証待ち` | `テスト中` または `保留` | 検証できる環境が用意されていないなら `保留` |

**この対応は一度決めて、残り全部へ同じように当てる。** タスクごとに違う判断をすると、後から状態の意味が読めなくなる。

### 気をつけること — 一度に1タスクか

`task-cycle` は「**一度に1タスク**」を定めているが、**この組織は並列実行を前提にしている。** 1つの担当エージェントが同時に持つのは1タスクだが、依存の無いタスクは同時に走る。

移行先のリポジトリに `task-cycle` の規約が残っていると、**どちらに従うか競合する。** 移行時に、そのリポジトリの `CLAUDE.md` から一度に1タスクの規定を外すか、この組織を使わないかを決めること。

## まだ入っていないもの

- 人間用ビュー（README / handbook）の生成スクリプト。`docs/decisions/06-documentation-agent.md` の確定事項 A

## 配布方式について

現状は手動コピーである。Claude Code のプラグイン機構でまとめれば「ファイル追加だけで導入できる」と「既存拡張と共存する」の両方をより強く満たせる可能性があるが、MVP では手動コピーで足りると判断した。**PO の判断を仰ぐべき論点として残してある**（`docs/decisions/13-execution-form.md` の未確定事項）。
