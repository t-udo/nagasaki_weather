# 長崎 天気予報比較

ウェザーニュース（長崎市）、Yahoo!天気（長崎市）、tenki.jp（ハウステンボス）の **9月20〜22日** 予報を1ページで比較します。

## 見る

`index.html` をブラウザで開くか、ローカルで:

```bash
python3 -m http.server 8765
```

[http://127.0.0.1:8765/](http://127.0.0.1:8765/)

## 更新する

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python update.py
```

対象日を変えるとき:

```bash
.venv/bin/python update.py --dates 2026-09-20,2026-09-21,2026-09-22
```

手元の cron なら `.venv/bin/python update.py --install-cron`（6/12/18/21時台）。WSL では `sudo service cron start` が必要なことがあります。

## GitHub Pages

GitHub Actions が1日4回予報を取り直して公開します。

1. このリポジトリを GitHub に push する
2. Settings → Pages → Source を **GitHub Actions** にする
3. Actions から `Update forecast and deploy Pages` を手動実行する

公開後の URL は `https://<ユーザー名>.github.io/<リポジトリ名>/` です。
