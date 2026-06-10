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
// Phase 1: Hook Mojo Send — 捕获所有 Mojo IPC 消息
// ============================================================

function hookMojoSend() {
    var mojoModule = Process.findModuleByName("mmmojo_64.dll");
    if (!mojoModule) {
        console.log("[!] mmmojo_64.dll not loaded yet, waiting...");
        return false;
    }

    console.log("[+] mmmojo_64.dll base: " + mojoModule.base);

    // Hook SendMMMojoWriteInfo
    var sendAddr = Module.findExportByName("mmmojo_64.dll", "SendMMMojoWriteInfo");
    if (sendAddr) {
        Interceptor.attach(sendAddr, {
            onEnter: function(args) {
                // args[0] = handle, args[1] = write_info struct
                this.handle = args[0];
                this.writeInfo = args[1];
            },
            onLeave: function(retval) {
                console.log("[Mojo Send] handle=" + this.handle + " result=" + retval);
            }
        });
        console.log("[+] Hooked SendMMMojoWriteInfo at " + sendAddr);
    }

    // Hook SwapMMMojoWriteInfoCallback — 拦截消息回调
    var swapAddr = Module.findExportByName("mmmojo_64.dll", "SwapMMMojoWriteInfoCallback");
    if (swapAddr) {
        Interceptor.attach(swapAddr, {
            onEnter: function(args) {
                this.info = args[0];
                this.callback = args[1];
                console.log("[Mojo SwapCallback] info=" + this.info + " callback=" + this.callback);
            }
        });
        console.log("[+] Hooked SwapMMMojoWriteInfoCallback at " + swapAddr);
    }

    // Hook SwapMMMojoWriteInfoMessage — 拦截消息数据
    var swapMsgAddr = Module.findExportByName("mmmojo_64.dll", "SwapMMMojoWriteInfoMessage");
    if (swapMsgAddr) {
        Interceptor.attach(swapMsgAddr, {
            onEnter: function(args) {
                this.info = args[0];
                this.buffer = args[1];
                this.size = args[2];
                // dump the protobuf message
                if (this.buffer && this.size && this.size.toInt32() > 0) {
                    var size = this.size.toInt32();
                    if (size > 10 && size < 100000) {
                        console.log("[Mojo Message] size=" + size);
                        console.log(hexdump(this.buffer, {length: Math.min(size, 512)}));
                    }
                }
            }
        });
        console.log("[+] Hooked SwapMMMojoWriteInfoMessage at " + swapMsgAddr);
    }

    return true;
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

// 等待 DLL 加载
var mojoHooked = false;
var checkInterval = setInterval(function() {
    if (!mojoHooked) {
        mojoHooked = hookMojoSend();
        if (mojoHooked) {
            hookSNSFunctions();
            searchMojoServices();
        }
    }
}, 2000);

// 30秒后停止检查
setTimeout(function() {
    clearInterval(checkInterval);
    if (!mojoHooked) {
        console.log("[!] mmmojo_64.dll not found after 30s. Is WeChat (Weixin.exe) running?");
    }
}, 30000);

console.log("[*] Hook script active. Trigger a Moments post to capture Mojo messages.");
