# 00. ベストプラクティス調査メモ

**Status:** Reference（決定ではなく、決定の根拠）
**調査日:** 2026-08-19
**目的:** 01〜06 の役割定義および未確定事項に、外部の知見を織り込む

> 本ファイルは根拠の記録である。ここに書かれた知見のうち、実際に仕様へ反映したものは各決定記録に「根拠」として明示している。

## 調査できなかった範囲

`anthropic.com` および `claude.com` は、本実行環境の egress proxy によりブロックされている（`code.claude.com` は到達可能）。Anthropic 公式記事の一次情報は検索結果の要約経由で取得しており、原文にあたれていない。将来、到達可能な環境で原典を確認することが望ましい。

---

## 1. Claude Code の標準機能（一次情報：code.claude.com）

### 1.1 サブエージェントの制約 — 組織設計を規定する

| 事実 | 組織設計への含意 |
| --- | --- |
| サブエージェントは **`AskUserQuestion` を使えない**（全サブエージェントで除外） | **PO への問い合わせはメインセッションからしか行えない。** PO確認待ちキューをオーケストレーターが一元管理する 01 の設計は、好みではなく技術的必然 |
| 各サブエージェントは**新規の隔離コンテキスト**で開始する。メイン会話履歴、auto memory を継承しない | 作業指示は**自己完結**でなければならない。「さっき話した件」は通じない（01 の O-2） |
| **CLAUDE.md 階層は継承される** | 組織共通の規約は CLAUDE.md / `.claude/rules/` に置けば、全エージェントが等しく参照する。エージェント定義ごとに重複記載しなくてよい |
| 結果はサマリとして返る | 出力を情報項目レベルで定義した 01〜06 の方針と整合 |
| `tools` / `disallowedTools` でツールを制限できる | オーケストレーターから編集系ツールを外せば、§6.3「実装しない」を**規約ではなく構成で**担保できる |
| `skills` フィールドでスキルをプリロードできる | 専門能力をスキルへ分離する §17.3 の実現手段 |
| `memory` フィールドでサブエージェント固有の永続メモリを持てる | §15 セッション引き継ぎの補助手段になりうる |
| ネストは既定で3層まで | 6ロール構成では問題にならない |
| サブエージェント間は `SendMessage` で通信できる | §14 の直接通信（実装↔レビュー、実装↔設計）は技術的に可能 |

### 1.2 CLAUDE.md / rules / Skills の使い分け

| 機構 | ロード | 適するもの |
| --- | --- | --- |
| `CLAUDE.md` | **毎セッション全文**。200行以下推奨（超えると遵守率が落ちる） | 常に必要な**事実**：規約、構成、「常に X せよ」 |
| `.claude/rules/*.md` | 毎セッション（`paths:` 指定時は該当ファイル操作時のみ） | トピック別に分割した規約 |
| Skills (`SKILL.md`) | **呼ばれた時のみ** | **手順**：多段階の作業、チェックリスト、長い参照資料 |
| hooks | ライフサイクルイベントで**必ず実行** | 強制したいもの |

決定的な区別: **CLAUDE.md は強制力を持たない**（コンテキストであって設定ではない）。必ず実行させたいことは hook にする。

「CLAUDE.md の一節が事実ではなく手順に育ったら、それは skill にすべき」— 公式ドキュメントの指針。§4.5 のトークン効率に直結する。

### 1.3 コンパクション

プロジェクトルートの CLAUDE.md はコンパクション後に再注入される。ネストした CLAUDE.md と `paths:` 付きルールは再注入されない。会話中だけで与えた指示は失われる。

→ §15 のセッション継続で「必ず残すべき情報」は、会話ではなくファイルへ置く必要がある。

---

## 2. マルチエージェント設計の知見（検索結果経由）

### 2.1 コスト構造

- Anthropic の研究システムは、通常のチャット比で **約15倍のトークン**を消費する
- **オーケストレーターのコンテキストは往復ごとに膨張する。** 3エージェントが各2,000トークンを返すと、1サイクルで6,000トークン増える
- ChatDev / MetaGPT のような大規模なエージェント群は通信コストが高い（HumanEval 1課題あたり $10 超の報告）。少人数構成はトークンを減らすが役割特化を犠牲にする
- **3〜5個の明確に定義されたエージェントが、十数個の曖昧なエージェントより高性能**

→ MVP の6ロールは妥当な規模。ただし報告の冗長さが直接コストになる（01 へ反映）。

### 2.2 失敗モード

| 失敗モード | 内容 | 反映先 |
| --- | --- | --- |
| **Context Overflow** | 会話履歴を要約せず渡すとコンテキストが溢れ、コストが爆発し、焦点を失う | 01：報告は要約。成果物は参照で渡す |
| **Incomplete Context Transfer** | 指示が曖昧・不完全でも、下流エージェントはエラーを上げずに誤った前提で進む | 01：着手前の指示不足チェックと差し戻し |
| **Early-Victory Problem** | 検証エージェントが1つ確認して成功を見ると、残りの失敗を見ずに止まる | 05：全完了条件の検証を必須化 |
| **Failure to Delegate** | オーケストレーターが委譲せず自分で作業を続けてしまう | 01：§6.3 の構成による担保 |
| **Context Loss During Handoffs** | 引き継ぎ時に文脈が失われ、重複作業や沈黙した失敗が起きる | 01：作業指示の自己完結性 |

### 2.3 評価ループが循環する条件

「evaluator-optimizer は、**評価者が出力の良し悪しを確実に判別できないとき循環する**」

→ 04 で確定した「2往復で打ち切ってオーケストレーターへ」は、この循環の検知手段として機能する。往復が収束しないこと自体が、判定基準（完了条件・設計）の不備を示す信号である。

### 2.4 状態の外部化

Anthropic の研究システムでは、リードエージェントが**コンテキストが埋まる前に計画をメモリへ保存する**。200,000トークンを超えるとコンテキストが切り詰められ、計画が失われるため。

→ §15 と 01 の設計（タスクリストは常に外部にある）を裏付ける。加えて、オーケストレーターは**計画を早期に外部化すべき**。

### 2.5 マルチエージェントが有利な条件

「アーキテクチャはタスク構造に従う。マルチエージェントが勝つのは、タスクが**独立した並列スレッドへ分解できるとき**のみ」

→ §13「PO確認待ち中も独立タスクは継続する」は、コスト面からも正当化される。逆に、直列依存だらけのゴールでは組織化の利得が出ない。オーケストレーターのタスク分解は、**並列実行できる単位を意図的に作る**べき。

### 2.6 役割特化型システムの実績と限界

ChatDev（設計→コーディング→テスト→ドキュメントの4段階）、MetaGPT（Product Manager / Architect / Project Manager / Engineer / QA の5役）は、本 MVP と近い構成を採る。本 MVP の6ロールはこの系譜にある。

限界として、複雑な課題（例：テトリスの実装）では10回試行しても中核機能が欠けるという報告がある。深い論理的推論と抽象化を要する課題は、役割分割だけでは解決しない。

→ ゴール健全性評価（§6.2）が「タスク消化数ではなくゴールへの前進」を見る設計は、この限界への対策として妥当。

---

## 出典

- [Claude Code: Subagents](https://code.claude.com/docs/en/sub-agents)
- [Claude Code: Skills](https://code.claude.com/docs/en/skills)
- [Claude Code: Memory](https://code.claude.com/docs/en/memory)
- [Anthropic's Multi-Agent Research Architecture Explained](https://theaiengineer.substack.com/p/how-anthropic-built-multi-agent-deep)
- [How Anthropic Built a Multi-Agent Research System (ByteByteGo)](https://blog.bytebytego.com/p/how-anthropic-built-a-multi-agent)
- [Multi-Agent Cost Compounding: Why 3 Agents Cost 10x (Augment Code)](https://www.augmentcode.com/guides/multi-agent-cost-compounding)
- [Multi-Agent Orchestration: A Practical Architecture Without the Buzzwords (Augment Code)](https://www.augmentcode.com/guides/multi-agent-orchestration-architecture-guide)
- [Multi-Agent Orchestration: How to Build Agent Teams That Actually Work (MindStudio)](https://www.mindstudio.ai/blog/multi-agent-orchestration-patterns)
- [Anthropic's Effective Agents Framework: A Pattern Map (AgentPatterns.ai)](https://agentpatterns.ai/patterns/agent-design/anthropic-effective-agents-framework/)
- [ChatDev: Communicative Agents for Software Development (arXiv)](https://arxiv.org/pdf/2307.07924)
- [LLM-based multi-agent systems for software engineering (SMU)](https://ink.library.smu.edu.sg/cgi/viewcontent.cgi?article=11489&context=sis_research)
