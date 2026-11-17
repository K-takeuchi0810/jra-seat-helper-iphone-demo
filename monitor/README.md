# 無料の外部稼働確認（準備済み・未設置）

Google Apps Scriptで、自分のRenderアプリの公開`/health`を5分ごとに確認する補助機能です。iPhoneやWindows PCが起動している必要はありません。Renderが無通信で休止することへの対策候補です。

**設置・連続稼働はまだ検証していません。** RenderやGoogleが再起動・制限・停止した場合の継続動作は保証しません。JRAの席種ページの確認や申し込みを行うスクリプトではありません。

## データと費用

- Renderの`https://自分のアプリ名.onrender.com/health`だけにGETリクエストを送ります。
- 認証情報・Cookie・JRAの会員情報・画面画像・席名は渡しません。
- HTTPS証明書を検証し、別URLへのリダイレクトはたどりません。
- Googleへの追加権限は、外部リクエストとこのプロジェクトのトリガー管理だけです。メール、カレンダー、Driveのファイル読み取り権限は宣言しません。
- 5分ごとの確認は1日288回です。Apps Scriptの一般アカウントのURL Fetch枠は現在20,000回/日ですが、同じアカウントのほかのスクリプトとの合計に上限が適用されます。
- この構成で有料APIや課金設定を有効にする必要はありません。RenderもFreeインスタンスのみを使い、支払方法を追加しません。
- 常時稼働させるとRenderの無料インスタンス時間を使います。ほかのサービスと共有される750時間/月を超えた場合などは、無料サービスが停止する場合があります。

## 設置（アプリを公開した後）

1. 自分のGoogleアカウントでApps Scriptの新規プロジェクトを作成します。
2. `Code.gs`と`appsscript.json`を設定します。このプロジェクトの公開や「ウェブアプリとしてデプロイ」は不要です。
3. プロジェクト設定のスクリプトプロパティに`SEAT_HELPER_HEALTH_URL`を追加し、実際に公開した自分のRenderアプリの`/health` URLを設定します。
4. `checkSeatHelperHealth`を実行し、必要な権限を自分で確認します。アプリのヘルスチェックが`{"ok":true}`を返す必要があります。
5. `installSeatHelperMonitor`を実行し、トリガー一覧に5分ごとの実行が1件だけ登録されたことを確認します。
6. 実行履歴で複数回の成功を確認し、その後iPhoneを閉じた状態で待機・通知を実機検証します。

使わなくなったときは`stopSeatHelperMonitor`を実行します。このスクリプトが使う名前のトリガーだけを削除し、ほかのトリガーには触れません。補助機能を止めると、Renderの無通信時の休止条件が再び問題になる場合があります。

出典：[Googleの定期トリガー](https://developers.google.com/apps-script/guides/triggers/installable)、[Googleの利用上限](https://developers.google.com/apps-script/guides/services/quotas)、[Render無料枠](https://render.com/docs/free)。
