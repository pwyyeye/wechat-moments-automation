/**
 * Frida Hook — 最底层 socket 拦截
 *
 * 微信 4.x 用 mmcronet.dll (Chromium 网络栈)，不走 WinHTTP/WinINet。
 * 但所有网络流量最终都要经过 ws2_32.dll 的 WSASend/send。
 * Hook 这些函数可以捕获所有外发数据。
 *
 * 使用:
 *   frida -p <PID> -l hook_socket.js
 */

// ============================================================
// Hook ws2_32.dll — 所有 socket 通信的必经之路
// ============================================================

function hookWinSock() {
    var ws2 = Process.findModuleByName("ws2_32.dll");
    if (!ws2) {
        console.log("[!] ws2_32.dll not found");
        return;
    }

    console.log("[+] ws2_32.dll base: " + ws2.base);

    var exports = ws2.enumerateExports().filter(function(e) { return e.type === 'function'; });

    // Hook send, WSASend, WSASendTo
    var targets = ['send', 'WSASend', 'WSASendTo'];
    var hooked = 0;

    exports.forEach(function(e) {
        if (targets.indexOf(e.name) >= 0) {
            try {
                var addr = e.address;
                var funcName = e.name;
                Interceptor.attach(addr, {
                    onEnter: function(args) {
                        var sock = args[0];
                        var buf, len;

                        if (funcName === 'send') {
                            buf = args[1];
                            len = args[2].toInt32();
                        } else if (funcName === 'WSASend' || funcName === 'WSASendTo') {
                            // WSASend(s, lpBuffers, dwBufferCount, ...)
                            var lpBuffers = args[1];
                            var dwCount = args[2].toInt32();
                            if (lpBuffers && dwCount > 0) {
                                // lpBuffers 指向 WSABUF 数组
                                // WSABUF: { len: u32, buf: pointer }
                                buf = lpBuffers.add(8).readPointer();  // offset 8 = buf
                                len = lpBuffers.readU32();              // offset 0 = len
                            }
                        }

                        if (buf && !buf.isNull() && len > 20 && len < 500000) {
                            try {
                                // 检查是否为 HTTP/HTTPS (以方法或 URL 开头)
                                var peek = buf.readByteArray(Math.min(len, 256));
                                var arr = new Uint8Array(peek);
                                var head = '';
                                for (var i = 0; i < Math.min(arr.length, 20); i++) {
                                    var b = arr[i];
                                    head += (b >= 32 && b < 127) ? String.fromCharCode(b) : '.';
                                }

                                // 过滤: 打印所有 HTTP 类请求 + 大包 + SNS 关键字
                                var full = '';
                                for (var i = 0; i < Math.min(arr.length, 200); i++) {
                                    var b = arr[i];
                                    full += (b >= 32 && b < 127) ? String.fromCharCode(b) : '.';
                                }

                                var isHTTP = (head.indexOf('POST') >= 0 || head.indexOf('GET') >= 0 ||
                                             head.indexOf('HTTP') >= 0 || head.indexOf('PUT') >= 0);
                                var isSNS = (full.indexOf('sns') >= 0 || full.indexOf('Sns') >= 0 ||
                                            full.indexOf('moments') >= 0 || full.indexOf('Moments') >= 0 ||
                                            full.indexOf('timeline') >= 0);
                                var isLarge = len > 500;

                                if (isHTTP || isSNS || isLarge) {
                                    console.log("\n[" + funcName + "] len=" + len +
                                               (isHTTP ? " [HTTP]" : "") +
                                               (isSNS ? " [SNS!]" : ""));
                                    console.log("  HEAD: " + head);
                                    if (isHTTP || isSNS) {
                                        console.log(hexdump(buf, {length: Math.min(len, 2048)}));
                                    }
                                }
                            } catch(ex) {}
                        }
                    }
                });
                hooked++;
                console.log("[+] Hooked " + funcName);
            } catch(ex) {
                console.log("[-] " + funcName + ": " + ex);
            }
        }
    });

    console.log("[+] Total ws2_32 hooks: " + hooked);
}

// ============================================================
// Hook securesocket / SSL (如果 ws2_32 抓到的是加密数据)
// ============================================================

function hookBCrypt() {
    // 微信 4.x 可能用 BCryptEncrypt/BCryptDecrypt 做 TLS
    var ncrypt = Process.findModuleByName("bcrypt.dll") ||
                 Process.findModuleByName("bcryptprimitives.dll");
    if (!ncrypt) return;

    console.log("[+] Found: " + ncrypt.name + " at " + ncrypt.base);

    var exports = ncrypt.enumerateExports().filter(function(e) { return e.type === 'function'; });
    exports.forEach(function(e) {
        if (e.name === 'BCryptEncrypt' || e.name === 'BCryptDecrypt') {
            try {
                var addr = e.address;
                var funcName = e.name;
                Interceptor.attach(addr, {
                    onEnter: function(args) {
                        // BCryptEncrypt(hKey, lpInput, cbInput, ...)
                        var inputLen = args[2].toInt32();
                        if (inputLen > 50 && inputLen < 100000 && args[1] && !args[1].isNull()) {
                            // 只记录大小，不 dump（加密数据无意义）
                            // 但标记为可能的 SNS 请求
                        }
                    }
                });
                console.log("[+] Hooked " + funcName);
            } catch(ex) {}
        }
    });
}

// ============================================================
// 主入口
// ============================================================

console.log("[*] Socket-Level Network Hook v0.1");
hookWinSock();
hookBCrypt();
console.log("[*] Ready. Post a Moments to capture socket data.");
