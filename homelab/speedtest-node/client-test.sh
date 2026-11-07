#!/bin/bash
# 客户端测速：两种方法
# 用法：
#   ./client-test.sh https://你的测速域名
#   ./client-test.sh http://你的VPS_IP:8080
set -e
URL="${1:?请传测速地址，例如 ./client-test.sh https://xxx.up.railway.app}"
echo "=== 1) 网页测速 ==="
echo "浏览器打开: $URL ，点 Start，约40秒出 ping / jitter / 下载 / 上传"
echo ""
echo "=== 2) 命令行测速 (librespeed-cli, 不用开浏览器) ==="
echo "先安装: go install github.com/librespeed/speedtest-cli@latest"
echo "或用 Docker:"
echo "  docker run --rm ghcr.io/librespeed/speedtest-cli --server $URL --telemetry-level disabled"
echo ""
echo "=== 3) 简单下载测速 (只测下载, 不耗上传流量) ==="
echo "  curl -o /dev/null -w '下载速度: %{speed_download} B/s, 用时: %{time_total}s\n' $URL/backend/garbage.php?ckSize=25"
echo ""
echo "=== 4) iperf3 测速 (需要服务端也装 iperf3, 最准, 适合测 VPS 直连) ==="
echo "  服务端: iperf3 -s -p 5201"
echo "  客户端: iperf3 -c 你的服务器IP -p 5201 -R   # -R 是测下载, 不加是测上传"
