/**
 * Frida Hook 脚本 — 拦截微信朋友圈相关的 Mojo IPC 调用
 *
 * 目标: 捕获发布朋友圈时的完整 protobuf 消息，用于逆向 XPlugin Mojo 接口
 *
 * 使用:
 *   frida Weixin.exe -l hook_sns.js
 *   或
 *   frida -p <微信PID> -l hook_sns.js
 *
 * Phase 1: Hook mmmojo_64.dll 的 Send 函数 → 捕获所有 Mojo 消息
 * Phase 2: 过滤出 SNS/朋友圈相关的消息
 * Phase 3: dump protobuf 原始数据
 */

// ============================================================
// Phase 0: 探明实际导出名
// ============================================================

function listMojoExports() {
    var mojoModule = Process.findModuleByName("mmmojo_64.dll");
    if (!mojoModule) return;

    console.log("[*] mmmojo_64.dll exports:");
    var exports = mojoModule.enumerateExports();
    exports.forEach(function(e) {
        if (e.type === 'function') {
            console.log("    " + e.name + " @ " + e.address);
        }
    });
}

// ============================================================
// Phase 1: Hook Mojo Send — 用实际导出名
// ============================================================

var MOJO_HOOKED = false;

function hookMojoSend() {
    if (MOJO_HOOKED) return true;

    var mojoModule = Process.findModuleByName("mmmojo_64.dll");
    if (!mojoModule) {
        return false;
    }

    console.log("[+] mmmojo_64.dll base: " + mojoModule.base);

    // 先列出实际导出
    listMojoExports();

    // Hook Send 函数 (所有包含 Send 的导出)
    var exports = mojoModule.enumerateExports();
    var hooked = 0;

    exports.forEach(function(e) {
        if (e.type !== 'function') return;

        // Hook 所有 Send 和 Swap 函数
        if (e.name.indexOf('Send') >= 0 || e.name.indexOf('Swap') >= 0) {
            try {
                Interceptor.attach(e.address, {
                    onEnter: function(args) {
                        var funcName = e.name;
                        // 只对 SwapMessage 打印 hexdump（最可能携带消息数据）
                        if (funcName.indexOf('Message') >= 0 && args[1] && !args[1].isNull()) {
                            try {
                                var size = args[2] ? args[2].toInt32() : 256;
                                if (size > 10 && size < 100000) {
                                    console.log("\n[Mojo " + funcName + "] size=" + size);
                                    console.log(hexdump(args[1], {length: Math.min(size, 512)}));
                                }
                            } catch(ex) {}
                        } else {
                            console.log("[Mojo] " + funcName + " called");
                        }
                    }
                });
                hooked++;
                console.log("[+] Hooked " + e.name + " at " + e.address);
            } catch(ex) {
                console.log("[-] Failed to hook " + e.name + ": " + ex);
            }
        }
    });

    if (hooked > 0) {
        MOJO_HOOKED = true;
        console.log("[+] Hooked " + hooked + " Mojo functions");
    }
    return MOJO_HOOKED;
}

// ============================================================
// Phase 2: Hook SNS 函数 — Weixin.dll 内的朋友圈处理
// ============================================================

function hookSNSFunctions() {
    var weixinModule = Process.findModuleByName("Weixin.dll");
    if (!weixinModule) {
        console.log("[!] Weixin.dll not loaded");
        return;
    }

    console.log("[+] Weixin.dll base: " + weixinModule.base);

    // 搜索 SNS 相关符号
    var exports = weixinModule.enumerateExports();
    var snsExports = exports.filter(function(e) {
        return e.name.indexOf("sns") >= 0 || e.name.indexOf("Sns") >= 0 || e.name.indexOf("SNS") >= 0;
    });

    console.log("[*] Found " + snsExports.length + " SNS-related exports:");
    snsExports.forEach(function(e) {
        console.log("    " + e.name + " @ " + e.address);
    });

    // 尝试 Hook sns_feedH (朋友圈 feed handler)
    var feedExport = exports.filter(function(e) { return e.name.indexOf("sns_feedH") >= 0; })[0];
    if (feedExport) {
        Interceptor.attach(feedExport.address, {
            onEnter: function(args) {
                console.log("[SNS Feed] called!");
                // args[0] 可能是 this 指针
                // args[1] 可能是 request protobuf
                console.log("  arg0=" + args[0] + " arg1=" + args[1] + " arg2=" + args[2]);
                if (args[1] && !args[1].isNull()) {
                    try {
                        // 尝试读取前512字节作为protobuf
                        console.log(hexdump(args[1], {length: 256}));
                    } catch(e) {}
                }
            }
        });
        console.log("[+] Hooked sns_feedH at " + feedExport.address);
    }
}

// ============================================================
// Phase 3: 搜索 Mojo service 名称
// ============================================================

function searchMojoServices() {
    var weixinModule = Process.findModuleByName("Weixin.dll");
    if (!weixinModule) return;

    // 搜索 "com.tencent" 字符串
    var pattern = "63 6F 6D 2E 74 65 6E 63 65 6E 74"; // "com.tencent" in hex
    var results = Memory.scanSync(weixinModule.base, weixinModule.size, pattern);

    console.log("[*] Found " + results.length + ' "com.tencent" references');

    var services = new Set();
    results.forEach(function(match) {
        try {
            var str = match.address.readCString(100);
            if (str && str.startsWith("com.tencent")) {
                services.add(str);
            }
        } catch(e) {}
    });

    console.log("[*] Mojo Services:");
    services.forEach(function(s) { console.log("    " + s); });
}

// ============================================================
// 主入口
// ============================================================

console.log("[*] WeChat SNS Mojo Hook v0.1");
console.log("[*] Waiting for mmmojo_64.dll...");

// 只尝试一次（DLL 已经加载了）
setTimeout(function() {
    if (hookMojoSend()) {
        hookSNSFunctions();
        searchMojoServices();
    }
}, 1000);

console.log("[*] Hook script active. Trigger a Moments post to capture Mojo messages.");
