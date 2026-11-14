import requests
import concurrent.futures
import time
import os

# Các nguồn cung cấp proxy miễn phí uy tín (API)
PROXY_SOURCES = [
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt"
]

TEST_URL = "http://httpbin.org/ip" # API để test proxy
TIMEOUT = 5 # Giây

def fetch_raw_proxies():
    print("🔄 Đang cào proxy từ các nguồn...")
    proxies = set()
    for url in PROXY_SOURCES:
        try:
            res = requests.get(url, timeout=10)
            if res.status_code == 200:
                lines = res.text.strip().split('\n')
                for line in lines:
                    line = line.strip()
                    if line and ":" in line:
                        proxies.add(line)
        except Exception as e:
            print(f"  ❌ Lỗi tải từ {url}: {e}")
    print(f"✅ Tìm thấy {len(proxies)} proxy thô.")
    return list(proxies)

def check_proxy(proxy):
    """Kiểm tra xem proxy có sống và kết nối được không"""
    proxy_dict = {
        "http": f"http://{proxy}",
        "https": f"http://{proxy}"
    }
    try:
        start = time.time()
        res = requests.get(TEST_URL, proxies=proxy_dict, timeout=TIMEOUT)
        if res.status_code == 200:
            latency = int((time.time() - start) * 1000)
            return proxy, latency
    except:
        pass
    return None, None

def main():
    raw_proxies = fetch_raw_proxies()
    if not raw_proxies:
        print("❌ Không lấy được proxy nào.")
        return

    print(f"🔄 Đang test tốc độ {len(raw_proxies)} proxy (Có thể mất 1-2 phút)...")
    good_proxies = []
    
    # Chạy đa luồng 50 luồng cùng lúc để test cho nhanh
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
        futures = {executor.submit(check_proxy, p): p for p in raw_proxies}
        
        count = 0
        for future in concurrent.futures.as_completed(futures):
            count += 1
            if count % 100 == 0:
                print(f"  ... Đã test {count}/{len(raw_proxies)}")
                
            proxy, latency = future.result()
            if proxy:
                good_proxies.append((proxy, latency))

    # Sắp xếp proxy theo tốc độ (nhanh nhất lên đầu)
    good_proxies.sort(key=lambda x: x[1])

    if good_proxies:
        print(f"\n🎉 Tìm thấy {len(good_proxies)} proxy SỐNG. Đang lưu vào proxies.txt...")
        
        # Lấy top 500 con nhanh nhất để đưa vào tool (tránh file quá to gây lag tool)
        top_proxies = good_proxies[:500] 
        
        with open("proxies.txt", "w", encoding="utf-8") as f:
            f.write("# Danh sách Proxy tự động tạo bởi get_proxies.py\n")
            f.write(f"# Thời gian tạo: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"# Tổng số proxy sống: {len(good_proxies)} (Đã lưu top 500 con nhanh nhất)\n\n")
            for p, latency in top_proxies:
                f.write(f"{p}\n")
                
        print("✅ HOÀN TẤT! Bây giờ bạn có thể khởi động lại tool main.py.")
        print("💡 Mẹo: Nên chạy file get_proxies.py này mỗi 2-3 tiếng để cập nhật proxy mới.")
    else:
        print("❌ Rất tiếc, không có proxy nào sống. Hãy thử chạy lại script này.")

if __name__ == "__main__":
    main()