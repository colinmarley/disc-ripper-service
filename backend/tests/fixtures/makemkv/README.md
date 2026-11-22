# MakeMKV robot-mode fixtures

`dvd_tv_real_info.txt` was captured from a drive (MakeMKV 2.0.0; the drive
serial is masked). The other files are **synthetic**: hand-written in the documented
`makemkvcon -r` format (`MSG` / `DRV` / `CINFO` / `TINFO` / `SINFO`), not
captured from a drive.

To add real ones, with a disc in the drive on the ripper host:

```
docker exec disc-ripper makemkvcon -r --noscan info disc:0 > <name>_info.txt
```

A stored scan's raw output is also available from the service itself:
`GET /disc/scans/{scan_id}/raw`.
