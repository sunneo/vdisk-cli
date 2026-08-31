# vdi — Virtual Disk Image toolkit

一支命令列工具：從資料夾建立 / 轉換虛擬磁碟映像（vmdk / vhdx / iso），
並且**像掛載一樣**直接對映像內部檔案系統（FAT16/FAT32/exFAT/ext2/3/4）做完整讀寫 CRUD，
另附 daemon 模式與 **MCP server 前端**，讓 Claude 或其他 AI 直接讀寫、解析映像內容。

完整設計見 [DESIGN.md](DESIGN.md)。

## 狀態

WSL 引擎已跑通完整端到端（見 `tests/e2e_wsl.py`）：從資料夾建 ext4 vmdk → 轉 vhdx →
one-shot `fs ls`/`read` → `serve` + session → 透過 session 與 MCP（外部 client）寫入真實映像並讀回。
exFAT 建立 + 讀寫也已驗證。

| 能力 | 命令 | 狀態 |
|------|------|------|
| 從資料夾建立 vmdk / vhdx（指定 fs） | `vdi image create ./f out.vmdk --fs ext4\|exfat\|... --size 4G` | ✅ WSL 引擎實測 |
| 格式轉換 vmdk↔vhdx↔raw↔qcow2 | `vdi image convert a.vmdk b.vhdx` | ✅ 實測（+ qemu-img check） |
| 映像內 CRUD | `vdi fs ls/stat/size/read/write/mkdir/rmdir/rm/rename/mv/cp-in/cp-out/chmod/chown/df` | ✅ 實測 |
| 映像內全文搜尋 | `vdi fs grep <pattern> <target> [--glob] [-i]` | ✅ 實測（appliance 端執行） |
| one-shot 自動重用 session | 有 `vdi serve` 在跑同一映像時，`vdi fs *` ~0.5s（免 appliance 開機） | ✅ 實測 |
| daemon + session 探索 | `vdi serve` / `vdi sessions` / `vdi session info\|stop` | ✅ 實測 |
| session 遠端 CRUD | `vdi fs ... --session NAME` 或 `NAME:/path` | ✅ 實測 |
| MCP 前端（給 AI 讀寫） | `vdi mcp <target>` / `vdi serve --mcp host:port` | ✅ 實測（TCP shim；官方 SDK stdio 選配） |
| 從資料夾建立 iso | `vdi image create out.iso` | 已接 xorriso via WSL，待實測 |
| 環境檢查 | `vdi doctor` | ✅ |
| 自帶 QEMU appliance（Windows 免 WSL） | `--engine qemu` | 待做 M0：`appliance/build.sh` |
| `local` 引擎（host 目錄當 fs，測試/除錯） | `--engine local` | ✅ 17 unit tests |

**已知限制**：沒有 `vdi serve` 在跑時，每個 one-shot `fs` 指令會重啟一次 libguestfs appliance（~50s，KVM 下較快）。
先開 `vdi serve <image>` 就會被後續 one-shot 自動重用（~0.5s）。大檔 cp-in/out 目前走 base64（roadmap：raw data channel）。

## 引擎

`vdi` 不依賴 host OS 的檔案系統驅動。它把目標映像掛給一個 Linux 環境，
用 Linux kernel 自己的驅動去讀寫，因此六種檔案系統在任何 OS 上都能讀能寫。

兩種引擎後端（同一介面，`--engine` 選擇）：

- **`wsl`**（現在可用）：透過既有的 WSL2 發行版執行 `libguestfs` / `guestfish`。
- **`qemu`**（發佈目標）：工具自帶 QEMU + 微型 Linux appliance，Windows 完全不需要 WSL。

`--engine auto` 會挑當下可用的。

## 安裝

```bash
python -m pip install -e .
vdi doctor
```

需要 Python 3.10+。`wsl` 引擎另需 WSL2 發行版做以下一次性設定：

```bash
# 1. 工具
sudo apt-get update
sudo apt-get install -y libguestfs-tools qemu-utils xorriso linux-image-generic
# 2. supermin 需要能讀 kernel image
sudo chmod 0644 /boot/vmlinuz-*
# 3. （建議）讓 appliance 走 KVM 而非慢速 TCG
sudo usermod -aG kvm "$USER"
printf '[boot]\ncommand = chmod 0666 /dev/kvm\n' | sudo tee /etc/wsl.conf
# 然後在 Windows: wsl --terminate <distro>
```

`vdi doctor` 會逐項檢查並在缺項時印出對應指令。

## 用法速覽

```bash
vdi doctor

vdi image create ./payload out.vhdx --fs ext4 --size 4G --label DATA
vdi image convert out.vhdx out.vmdk
vdi image parts out.vmdk

vdi fs ls   out.vmdk@1:/ -l
vdi fs read out.vmdk:/etc/hostname
vdi fs write out.vmdk:/etc/hostname --content "myhost"
vdi fs cp-in ./localdir out.vmdk:/opt/app -r

vdi serve out.vmdk --name build01 --mcp 127.0.0.1:7333
vdi sessions
vdi fs ls --session build01:/opt

# 讓 Claude 直接存取
vdi mcp out.vmdk@1
```
