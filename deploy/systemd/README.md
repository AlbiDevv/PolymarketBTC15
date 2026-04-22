Copy these unit templates to `/etc/systemd/system/` on Ubuntu:

- `prediction-shadow-lab.service`
- `prediction-dashboard.service`
- `prediction-telegram.service`

Expected app path:

```text
/home/trader/apps/prediction_trader
```

If your path differs, update `WorkingDirectory`, `Environment`, `ExecStart`, and `ReadWritePaths` in all three units before enabling them.
