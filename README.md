# 群獅管家 Bot

> AI管家 × AI業務 × AI CRM — 從第一次成交到第N次成交

---

## 產品定位

| 模組 | 說明 | 啟動方式 |
|---|---|---|
| **AI管家**（基礎包）| 每日捕捉、整理、早報、週報 | 預裝，自動啟動 |
| **AI業務包** | 潛客追蹤、成交記錄、業務管線 | 建立 `leads/` 資料夾 |
| **AI CRM包** | 客戶建檔、諮詢記錄、跟進提醒 | 建立 `clients/` 資料夾 |

---

## 快速部署

### 1. Fork 此 repo

### 2. 填寫 3 個設定檔
- `identity/background.md` — 使用者是誰
- `context/business-model.md` — 在做什麼生意
- `context/focus.md` — 每天早報追蹤什麼

### 3. 設定環境變數（參考 `bot/.env.example`）

```
TELEGRAM_TOKEN=
TELEGRAM_CHAT_ID=
GEMINI_API_KEY=
ANTHROPIC_API_KEY=
GITHUB_TOKEN=
GITHUB_OWNER=
GITHUB_REPO=
```

### 4. 部署到 Zeabur
連結此 repo → 填環境變數 → Deploy

---

## 加購模組開通

### AI業務包
在 repo 新增 `leads/README.md` → 重新部署 → 自動啟用

### AI CRM包
在 repo 新增 `clients/README.md` → 重新部署 → 自動啟用

---

## Bot 指令

| 指令 | 說明 |
|---|---|
| `/status` | 案子狀態 + 今日代辦 |
| `/test` | 立即觸發早報 |
| `/myid` | 取得 Chat ID |
| `/newclient 小美` | 建立客戶（CRM包）|
| `/client 小美` | 查看客戶資料（CRM包）|
| `/clients` | 客戶清單（CRM包）|

---

## 資料夾結構

```
bot/              程式碼
context/          商業背景 + 早報主題
identity/         使用者身份
inbox/            臨時收件匣（Bot 自動管理）
knowledge/
  ├── areas/      領域知識
  └── resources/  參考資料
memory/
  ├── daily/      每日日誌
  ├── weekly/     週報存檔
  └── decisions/  重要決策
projects/         進行中案子
sop/              可複製流程
zettel/           永久知識卡片
clients/          【CRM包】客戶資料
leads/            【業務包】潛客資料
```

---

授權與維護：群獅整合行銷
