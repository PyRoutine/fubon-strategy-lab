# Fubon Strategy Lab

公開的純策略測試實驗室。來源為 private 專案的 `feat/sop-pure-strategy-20260923` 分支，但此 repository 採乾淨初始化，不保留原 repository 的 Git history。

## 範圍

此 repository **只包含純策略決策與匿名測試**：

- LOW / HIGH mode
- 升溫區處理
- P1～P4 資金來源排序
- 最高 5X 輪動倍率
- 整股 / 零股市場選擇
- NAV 與折溢價相關判斷
- 雙股螺旋策略
- GitHub Actions 單元測試

## 明確不包含

- 富邦證券 API / SDK / 憑證
- 真實下單或券商連線
- Cloud Run / Cloud Build
- Firestore / Google Cloud
- LINE Bot / Webhook
- Secrets / tokens / 正式 endpoint
- 真實帳戶、庫存或 ARK 快照

## 執行測試

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```

策略程式不得直接連線外部服務或建立真實委託；正式執行層留在 private repository。
