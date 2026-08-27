# Changelog

narumi（narumi.app / pipeline / server）の変更履歴。最新の版を先頭に `## <version>` の見出しで書く。
`VERSION`・`pipeline/pyproject.toml`・`server/pyproject.toml` と最新見出しの版は常に一致させる
（`scripts/check-version.sh` が検査し、`scripts/release-app.sh` がリリースノートとして抜粋する）。

## 0.1.0 - 未リリース

- 初回リリース
- ワンボタン録画（画面・システム音声・マイクを別トラックで録画）とメニューバーアプリ narumi.app
- MCP サーバー（契約駆動のツール登録、Streamable HTTP / stdio）
- gaia-library の実契約・Bearer 認証に対応したブリーフ生成と議事録の提案登録
- アプリ・MCP・CLI からの Gaia 接続設定、キー管理、接続テスト
- 接続先と scope の検証、秘密入力・応答・ログの保護、実サーバー結合テスト
- 自己完結 .app（uv・wheels・requirements・contracts のランタイム同梱）と Sparkle 自動更新の配布基盤
