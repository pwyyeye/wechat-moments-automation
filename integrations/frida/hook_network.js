/**
 * Frida Hook — 拦截微信朋友圈的网络请求
 *
 * 朋友圈发布不走 Mojo IPC，但一定走 HTTPS 网络请求。
 * Hook WinHTTP/WinINet 的发送函数，捕获 Moments 发布的完整 HTTP 请求。
 *
 * 微信 PC 端网络栈:
 *   3.x 用 WinHTTP (winhttp.dll)
 *   4.x 用 Chromium 网络栈 (mmcronet.dll / 自实现) + TLS
 *
 * 使用:
 *   frida -p <PID> -l hook_network.js
 */

// ============================================================
// Hook 1: WinHTTP (微信 3.x)
// ============================================================

function hookWinHTTP() {
    var winhttp = Process.findModuleByName("winhttp.dll");
    if (!winhttp) {
        console.log("[!] winhttp.dll not loaded");
        return false;
    }

    console.log("[+] winhttp.dll base: " + winhttp.base);

    // 先列出实际导出
    var allExports = winhttp.enumerateExports().filter(function(e) { return e.type === 'function'; });
    var sendFuncs = allExports.filter(function(e) {
        return e.name.indexOf('Send') >= 0 || e.name.indexOf('send') >= 0;
    });
    console.log("[*] winhttp.dll Send functions:");
    sendFuncs.forEach(function(e) { console.log("    " + e.name); });

    // Hook 所有 Send 函数
    var hooked = 0;
    sendFuncs.forEach(function(e) {
        try {
            Interceptor.attach(e.address, {
                onEnter: function(args) {
                    var funcName = e.name;
                    // args[0]=hRequest, args[1]=headers, args[2]=headersLen, args[3]=body, args[4]=bodyLen
                    var bodyLen = args[4] ? (args[4].toInt32 ? args[4].toInt32() : 0) : 0;
                    if (bodyLen > 10 && bodyLen < 500000 && args[3] && !args[3].isNull()) {
                        console.log("\n[WinHTTP::" + funcName + "] body=" + bodyLen + " bytes");
                        console.log(hexdump(args[3], {length: Math.min(bodyLen, 1024)}));
                        // 也打印 headers
                        var hdrLen = args[2] ? (args[2].toInt32 ? args[2].toInt32() : 0) : 0;
                        if (hdrLen > 0 && args[1] && !args[1].isNull()) {
                            try {
                                console.log("[Headers] " + args[1].readUtf16String(Math.min(hdrLen, 500)));
                            } catch(ex) {}
                        }
                    }
                }
            });
            hooked++;
        } catch(ex) {}
    });

    if (hooked === 0) {
        // 回退: 直接 Hook WinHttpSendRequest 使用地址
        for (var i = 0; i < allExports.length; i++) {
            if (allExports[i].name === 'WinHttpSendRequest') {
                try {
                    var addr = allExports[i].address;
                    Interceptor.attach(addr, {
                        onEnter: function(args) {
                            var bodyLen = args[4].toInt32();
                            if (bodyLen > 10 && bodyLen < 500000) {
                                console.log("\n[WinHttpSend] body=" + bodyLen);
                                console.log(hexdump(args[3], {length: Math.min(bodyLen, 1024)}));
                            }
                        }
                    });
                    hooked++;
                    console.log("[+] Hooked WinHttpSendRequest at " + addr);
                } catch(ex) {
                    console.log("[-] Failed: " + ex);
                }
                break;
            }
        }
    }

    console.log("[+] Hooked " + hooked + " WinHTTP functions");
    return true;
}

// ============================================================
// Hook 2: WinINet (备选网络栈)
// ============================================================

function hookWinINet() {
    var wininet = Process.findModuleByName("wininet.dll");
    if (!wininet) return false;

    console.log("[+] wininet.dll base: " + wininet.base);
    var exports = wininet.enumerateExports().filter(function(e) { return e.type === 'function'; });
    var sendFuncs = exports.filter(function(e) {
        return e.name.indexOf('Send') >= 0 || e.name.indexOf('send') >= 0;
    });

    var hooked = 0;
    sendFuncs.forEach(function(e) {
        try {
            var addr = e.address;
            var funcName = e.name;
            Interceptor.attach(addr, {
                onEnter: function(args) {
                    var bodyLen = args[4] ? (args[4].toInt32 ? args[4].toInt32() : 0) : 0;
                    if (bodyLen > 10 && bodyLen < 500000 && args[3] && !args[3].isNull()) {
                        console.log("\n[WinINet::" + funcName + "] body=" + bodyLen);
                        console.log(hexdump(args[3], {length: Math.min(bodyLen, 1024)}));
                    }
                }
            });
            hooked++;
        } catch(ex) {}
    });
    console.log("[+] Hooked " + hooked + " WinINet functions");
    return true;
}

// ============================================================
// Hook 3: SSL_write (最底层 — 捕获所有加密前的数据)
// ============================================================

function hookSSLWrite() {
    var candidates = ["libcrypto-1_1-x64.dll", "libssl-1_1-x64.dll",
                      "sspicli.dll", "ncrypt.dll", "schannel.dll"];

    candidates.forEach(function(dllName) {
        var mod = Process.findModuleByName(dllName);
        if (!mod) return;

        console.log("[+] Found " + dllName + " at " + mod.base);
        var exports = mod.enumerateExports();
        var hooked = 0;
        exports.forEach(function(e) {
            if (e.type !== 'function') return;
            if (e.name === 'SSL_write' || e.name === 'ssl_write' ||
                e.name.indexOf('EncryptMessage') >= 0 || e.name === 'EncryptFile') {
                try {
                    var addr = e.address;
                    var funcName = e.name;
                    var dll = dllName;
                    Interceptor.attach(addr, {
                        onEnter: function(args) {
                            // SSL_write(fd, buf, len)
                            var buf = args[1];
                            var len = args[2] ? (args[2].toInt32 ? args[2].toInt32() : 0) : 0;
                            if (buf && !buf.isNull() && len > 20 && len < 100000) {
                                try {
                                    var data = buf.readByteArray(Math.min(len, 512));
                                    console.log("\n[SSL " + dll + "::" + funcName + "] len=" + len);
                                    console.log(hexdump(buf, {length: Math.min(len, 1024)}));
                                } catch(ex) {}
                            }
                        }
                    });
                    hooked++;
                    console.log("[+] Hooked " + dllName + "::" + e.name);
                } catch(ex) {
                    console.log("[-] " + dllName + "::" + e.name + ": " + ex);
                }
            }
        });
        if (hooked === 0) console.log("[*] " + dllName + ": no SSL write exports found");
    });
}

// ============================================================
// Hook 4: 扫描 WeChat 自己的网络模块
// ============================================================

function hookWeChatNetworking() {
    ['mmcronet.dll', 'ilink2.dll', 'ilink_stream.dll', 'TRAE.dll'].forEach(function(name) {
        var mod = Process.findModuleByName(name);
        if (!mod) return;
        console.log("[+] Found: " + name);

        var exports = mod.enumerateExports().filter(function(e) { return e.type === 'function'; });
        var sendFuncs = exports.filter(function(e) {
            var n = e.name;
            return (n.indexOf('Send') >= 0 || n.indexOf('Write') >= 0) && n.length < 80;
        });

        console.log("[*] " + name + ": " + sendFuncs.length + " send/write functions");
        var hooked = 0;
        sendFuncs.forEach(function(e) {
            try {
                var addr = e.address;
                var funcName = e.name;
                var modName = name;
                Interceptor.attach(addr, {
                    onEnter: function(args) {
                        // 静默 Hook，等待数据出现
                    }
                });
                hooked++;
            } catch(ex) {}
        });
    });
}

// ============================================================
// 主入口
// ============================================================

console.log("[*] WeChat Network Hook v0.1");
console.log("[*] Searching for network APIs...");

hookWinHTTP();
hookWinINet();
hookSSLWrite();
hookWeChatNetworking();

console.log("[*] Ready. Post a Moments to capture network requests.");
