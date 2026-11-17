# 開発版の検証記録

確認日：2026年11月17日（日本時間）

## 確認できたこと

- Pythonテスト16件が成功：認証付き状態・画面API、Cookieの失効、別サイトからの書き込み防止、ログイン試行制限、到着確認に対応するテストID、重複開始防止、中止、通知失敗時のブラウザ状態保持など。
- Node.jsテスト9件が成功：通知用公開鍵の再現性、送信先の制限、実ライブラリによる通知本文の暗号化、不正な購読鍵の拒否に加え、外部稼働確認の宛先制限、HTTPS・リダイレクト無効、未起動ページの誤判定防止、5分トリガーの重複防止、停止時に無関係なトリガーを残すこと。
- 実際のChromeを使い、ローカル模擬ページで希望席2席・前列の申し込み後に購入画面を検出。購入確定ボタンが押されていないことを確認。
- ユーザー提供の確保不可文言を模擬ページで再現し、購入手続きの通知イベントが発火しないことを確認。
- 幅393pxのブラウザでスマートフォン用画面を確認。画面から開始日時・枚数・エリアの設定、条件確認、開始を操作。
- 同じスマートフォン用画面の画像タップを使って模擬ページの注意事項を確認し、購入手続きの確認待ちまで進むことを確認。
- 通知未登録の場合に送信成功と表示しないことを確認。
- 実際のHTTP API・処理スレッド・Chromeを組み合わせ、クライアントの接続を切って定期取得もしない間に、デモの申し込みが購入確認画面へ進み、対応する通知イベントが1回だけ発行されることを確認。これはローカルサーバーでの検証であり、無料ホスティングの無通信停止条件とは別の確認。
- Linux用のイメージを構築し、512MiB・0.1CPUの制限、外部ネットワークなしでHTTPヘルスチェックを確認。
- 標準Docker設定ではChromiumの起動が失敗することを確認。`chromium-sandbox`を追加し、Microsoft公式のseccompプロファイルでユーザー名前空間の作成を許可した環境では、非rootのChromiumが起動してデモの前列2席を申し込み、購入前に停止。ブラウザ自身の診断でPID・ネットワーク名前空間とSeccomp-BPFの隔離が有効。`--no-sandbox`・`--privileged`・`seccomp=unconfined`は不使用。

## まだ証明できていないこと

- iPhone実機・ホーム画面アプリからのログイン、ブラウザ操作と通知許可。
- iPhoneのロック画面やアプリを閉じた状態へのプッシュ通知の到着。
- Render固有の起動条件で、必要な隔離機能を維持したブラウザ動作。ローカルDockerでの成功をRenderでの成功とは扱っていない。
- 無料クラウドでの長時間待機。Render無料Webサービスの無通信時停止条件は解消していない。
- Google Apps Scriptの実際のプロジェクト作成・権限承認・定期実行。`monitor/`のテストはGoogleのサービスを模した環境で行っており、実際のクラウドでの動作とは区別している。
- クラウド上のブラウザから実際のJRAサイトへの申し込み。

以上のテスト合格は、iPhone単体・無料で長時間監視し、確保時に必ず通知するという目標の達成を証明するものではありません。

## 指定枚数を確保できない場合の再確認

- 明確な確保不可だけ再確認し、枚数・エリアを維持して60秒以上待機することをテスト。
- GETで一覧へ戻り、失敗したPOSTを再読み込みしないことを確認。
- 定期確認オフ、結果不明、支払い・追加確認との併存、監視期限、中止時は再送しないことをテスト。
- 実サイトでの確保不可から再確認までの一連の動作は未検証です。

- おまかせ席選択の「最大1席」に対して2席を指定した場合、1席ボタンを押さず、再試行せずに条件見直しのため停止することをローカルのChromeと模擬ページで確認。

## 日本語ログインと混雑画面（2026-11-17）

- ブラウザ言語が日本語であること、日本語の会員番号欄へフォーカスし、資格情報の入力・ログイン送信を行わないことを検証。
- 混雑・順番待ちを購入確認や空席不足と扱わず、画面を維持することを検証。
- Python 35件を実行し、34件成功。既存のCSRFテスト1件がWindowsソケット切断でエラーとなったため、その1件だけを再実行し成功。

## アプリのログイン保持（2026-11-17）

- Python全体42件成功。その後、端末登録にログイン鍵の確認を追加し、関連8件を再実行して成功（合計43種類を検証）。既存のNode.js 9件も成功。
- 端末登録の認証必須、秘密鍵データの拒否、Origin制限、一度限りのチャレンジ、期限切れ、不正署名・証明改変、ログアウト後の無効化を検証。
- Cookieだけのコピーではログイン不可。サーバーの認証状態を破棄した後も、同じ端末による署名付きの復元は成功。
- ローカルのブラウザUIで保持を選択してログインし、テストサーバーのプロセスを終了・再起動してからページを読み込み直し、ログイン鍵なしで復元できることを確認。
- 同じブラウザUIでログアウトし、ページを読み込み直してもログイン画面のままであることを確認。
- iPhone実機での最大30日の保持と、長期間放置後の復元は未検証。端末のサイトデータが削除された場合などは再ログインが必要。

## 10秒間隔の設定（2026-11-17）

- 再確認関連12件、バックエンド関連14件の計26件が成功。
- 7秒の確認処理なら3秒待って次の確認を開始し、開始間隔が10秒となることを仮想時刻で検証。
- 確認処理が13秒かかった場合、並列化や遅れを埋める連打を行わず、完了後に次へ進むことを検証。
- 明確な確保不可の回答後は、その回答から10秒以上待つことを検証。
- APIは10秒を受け付け、10秒未満を拒否。実サイトへの10秒間隔の負荷テストは実施していません。

## JRAログイン欄の入力と消去（2026-11-17）

- Python全体53件成功。停止後の入力補正を加えて関連10件成功、その後に長すぎるアプリ鍵の誤転送防止1件も成功（合計55種類を検証）。
- ID・パスワードの上書き、一括消去、通常入力欄の消去、ログインを送信しないこと、読み取り専用欄の拒否、制御文字の拒否を検証。
- 状態APIと操作完了の通知に、入力したID・パスワードが含まれないことを検証。
- ローカルのブラウザUIでテスト用ID・パスワードを入力し、両欄の反映と消去の完了通知を確認。
- 実在のJRA会員番号・パスワードはテストに使用していません。iPhoneのJRA保存項目による自動入力は実機確認が必要です。

## Automatic seat notice flow
- 23 browser/retry tests passed on local fixtures (no live application).
- Seat-specific checkbox and next button advance automatically; exact quantities 1/2/3/4 reach payment without purchase.
- Unsupported checkbox stops; explicit stock rejection does not emit payment success; quantity limits and retry spacing preserved.


## Cloud payment authentication bridge (2026-11-17, local only)

- 17 remote-input tests passed, including same/cross-origin iframe input, window selection, prior input safety and foreground checks.
- 3 additional checks passed for inspection without navigation, closed popup recovery, and screenshot metadata.
- UI fixture confirmed two-window switching and input into an embedded mock authentication field. No real card, real OTP, issuer page or live purchase was used.
- Actual Mastercard ID Check success remains unverified. Deployment/server restart not performed.

## Authentication cookie compatibility
- Local synthetic cross-site iframe: incognito cookie=""; normal cookie="qa3ds=dummy-only". No real payment or credentials.
- 3 checks passed: default non-incognito temporary profile without security bypass flags, 1–4 seat flow, cross-origin authentication input.
- Actual issuer transaction remains unverified. No live restart or submission performed.


## Goal: OTP authentication and return to booking confirmation (2026-11-18)

- A local synthetic issuer iframe accepted a manually entered dummy code and navigated back to a separate mock merchant confirmation page. Passed. No email/card/real payment was used.
- Response error extraction tests passed: resource type and HTTP method retained, whitelisted service error codes extracted after Network.loadingFinished without replay, response messages/transaction tokens/OTP not exposed.
- JavaScript diagnostics test passed: only error categories retained, no full exception text.
- Existing JPEG/input and network redaction checks passed before the parser correction; the corrected parser's three unit checks were rerun and passed.
- Live transaction remains unresolved: observed as-pgx HTTP400; exact response code still unknown. This is not a successful payment verification.
- New diagnostic code is staged, not deployed; current transaction is preserved.
