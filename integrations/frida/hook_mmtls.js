/**
 * Hook mmtls 加密层 — 在微信加密前捕获明文 protobuf
 *
 * 微信 mmtls (MicroMessenger TLS) 是自定义传输加密层。
 * 数据在发送到 socket 前会被加密。
 * Hook Weixin.dll 中的 mmtls 相关函数，在加密前 dump 明文。
 *
 * 使用:
 *   frida -p <PID> -l hook_mmtls.js
 */

function hookMMTLS() {
    var weixin = Process.findModuleByName("Weixin.dll");
    if (!weixin) { console.log("[!] Weixin.dll not found"); return; }

    console.log("[+] Weixin.dll: " + weixin.base);

    // 搜索 mmtls 相关导出
    var exports = weixin.enumerateExports().filter(function(e) { return e.type === 'function'; });

    // 搜索 mmtls / crypt / encrypt 相关符号
    var targets = [];
    exports.forEach(function(e) {
        var n = e.name.toLowerCase();
        if (n.indexOf('mmtls') >= 0 || n.indexOf('mmlogin') >= 0 ||
            (n.indexOf('encrypt') >= 0 && n.length < 80) ||
            (n.indexOf('crypt') >= 0 && n.indexOf('crypt') < 10 && n.length < 60) ||
            (n.indexOf('pack') >= 0 && n.indexOf('pack') < 5 && n.length < 60) ||
            (n.indexOf('build') >= 0 && n.indexOf('request') >= 0 && n.length < 80) ||
            (n.indexOf('serialize') >= 0 && n.length < 60) ||
            (n.indexOf('mars') >= 0 && n.indexOf('stn') >= 0)) {
            targets.push(e);
        }
    });

    console.log("[*] Potential mmtls/crypto targets: " + targets.length);
    targets.forEach(function(e) { console.log("    " + e.name); });

    // Hook 加密相关函数 — 在入口处 dump 参数
    var hooked = 0;
    targets.forEach(function(e) {
        try {
            var addr = e.address;
            var funcName = e.name;
            Interceptor.attach(addr, {
                onEnter: function(args) {
                    // 尝试 dump 第2、3个参数（通常是 buffer + len）
                    for (var i = 1; i <= 3; i++) {
                        if (args[i] && !args[i].isNull()) {
                            try {
                                var peek = args[i].readByteArray(64);
                                if (peek) {
                                    var arr = new Uint8Array(peek);
                                    var hasText = false;
                                    for (var j = 0; j < arr.length; j++) {
                                        if (arr[j] >= 0x20 && arr[j] < 0x7f) hasText = true;
                                    }
                                    if (hasText) {
                                        console.log("\n[" + funcName + "] arg" + i + " possible plaintext:");
                                        console.log(hexdump(args[i], {length: 256}));
                                        // 尝试读取前面的 size 字段
                                        var maybeLen = args[i-1];
                                        if (maybeLen && typeof maybeLen === 'number') {
                                            console.log("  (arg" + (i-1) + " = " + maybeLen + ")");
                                        }
                                    }
                                }
                            } catch(ex) {}
                        }
                    }
                }
            });
            hooked++;
        } catch(ex) {}
    });

    console.log("[+] Hooked " + hooked + " mmtls/crypto functions");
}

// 也扫描整个内存空间中的 "mmtls" 字符串附近
function findMMTLSStrings() {
    var ranges = Process.enumerateRanges('r--');
    var found = 0;
    ranges.forEach(function(range) {
        if (found > 20) return;
        try {
            Memory.scan(range.base, range.size, "6d 6d 74 6c 73", {  // "mmtls"
                onMatch: function(address, size) {
                    if (found < 20) {
                        console.log("[mmtls string] at " + address);
                        found++;
                    }
                    return found < 20 ? 'stop' : undefined;
                },
                onComplete: function() {}
            });
        } catch(e) {}
    });
}

console.log("[*] mmtls Encryption Hook v0.1");
hookMMTLS();
console.log("[*] Searching for mmtls strings...");
setTimeout(findMMTLSStrings, 2000);
console.log("[*] Ready. Post a Moments to capture plaintext.");
