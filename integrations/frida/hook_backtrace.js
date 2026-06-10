/**
 * 调用栈回溯 — 从 socket send 反向追踪 mmtls 加密函数
 *
 * 策略: 在 WSASend/send 被调用时（发送到 extshort.weixin.qq.com），
 * 用 Thread.backtrace() 抓取完整调用栈。
 * 栈上的每个地址对应 Weixin.dll 内部的一个函数，
 * 在那些地址附近的代码就是 mmtls 加密/序列化逻辑。
 *
 * 使用: frida -p <PID> -l hook_backtrace.js
 */

var weixin = Process.findModuleByName("Weixin.dll");
var ws2 = Process.findModuleByName("ws2_32.dll");

if (!weixin) throw new Error("Weixin.dll not loaded");
if (!ws2) throw new Error("ws2_32.dll not loaded");

console.log("[+] Weixin.dll: " + weixin.base + " size=" + (weixin.size/1024/1024).toFixed(0) + "MB");
console.log("[+] ws2_32.dll: " + ws2.base);

// ============================================================
// 找到 send 和 WSASend
// ============================================================

var backtraces = [];
var captureCount = 0;
var MAX_CAPTURES = 5;

function hookSocketSend() {
    var exports = ws2.enumerateExports().filter(function(e) { return e.type === 'function'; });

    ['send', 'WSASend'].forEach(function(funcName) {
        var exp = null;
        for (var i = 0; i < exports.length; i++) {
            if (exports[i].name === funcName) { exp = exports[i]; break; }
        }
        if (!exp) return;

        Interceptor.attach(exp.address, {
            onEnter: function(args) {
                // 只捕获发给微信服务器的包
                if (captureCount >= MAX_CAPTURES) return;

                var buf, len;
                if (funcName === 'send') {
                    buf = args[1];
                    len = args[2].toInt32();
                } else {
                    // WSASend: lpBuffers[0]
                    var lpBuffers = args[1];
                    if (lpBuffers && !lpBuffers.isNull()) {
                        len = lpBuffers.readU32();
                        buf = lpBuffers.add(8).readPointer();
                    }
                }

                // 过滤: 只看大包 (可能是朋友圈发布)
                if (!buf || buf.isNull() || len < 5000 || len > 500000) return;

                // 检查是否是 HTTP (POST /mmtls)
                try {
                    var peek = buf.readByteArray(Math.min(len, 256));
                    var arr = new Uint8Array(peek);
                    var head = '';
                    for (var j = 0; j < Math.min(arr.length, 50); j++) {
                        var b = arr[j];
                        head += (b >= 32 && b < 127) ? String.fromCharCode(b) : '.';
                    }

                    // 只捕获 mmtls 请求
                    if (head.indexOf('mmtls') < 0) return;

                    captureCount++;
                    console.log("\n========================================");
                    console.log("[CAPTURE #" + captureCount + "] len=" + len + " head=" + head.substring(0, 40));
                    console.log("========================================");

                    // ★ 核心: 抓取调用栈
                    var bt = Thread.backtrace(this.context, Backtracer.ACCURATE);
                    console.log("[Backtrace] " + bt.length + " frames:");

                    var weixinFrames = [];
                    for (var i = 0; i < bt.length; i++) {
                        var addr = bt[i];
                        var moduleName = "???";
                        var offset = addr;

                        // 判断地址属于哪个模块
                        try {
                            var mod = Process.findModuleByAddress(addr);
                            if (mod) {
                                moduleName = mod.name;
                                offset = addr.sub(mod.base);
                            }
                        } catch(e) {}

                        // 尝试解析符号
                        var symbol = "";
                        try {
                            var sym = DebugSymbol.fromAddress(addr);
                            if (sym && sym.name) symbol = sym.name;
                        } catch(e) {}

                        console.log("  #" + i + "  " + addr + "  [" + moduleName + "+0x" + offset.toString(16) + "] " + symbol);

                        // 收集 Weixin.dll 内的地址
                        if (moduleName === 'Weixin.dll') {
                            weixinFrames.push({index: i, addr: addr, offset: offset});
                        }
                    }

                    console.log("\n[Weixin.dll frames to hook next]:");
                    weixinFrames.forEach(function(f) {
                        console.log("  frame #" + f.index + "  Weixin.dll+0x" + f.offset.toString(16));
                    });

                    // 对 Weixin.dll 中的地址设置一次性断点
                    // （下次触发时会停在更接近加密函数的地方）
                    if (weixinFrames.length > 0 && backtraces.length < 1) {
                        backtraces.push(weixinFrames);
                        console.log("\n[*] 这些偏移地址就是 mmtls 加密调用链中的函数。");
                        console.log("[*] 下一步: 在 Weixin.dll+这些偏移处 Hook，dump 参数。");
                    }

                } catch(e) {
                    console.log("[!] Error: " + e);
                }
            }
        });
        console.log("[+] Hooked " + funcName);
    });
}

// ============================================================
// 主入口
// ============================================================

console.log("[*] Call-Stack Backtrace Hook v0.1");
console.log("[*] Will capture up to " + MAX_CAPTURES + " mmtls sends");
hookSocketSend();
console.log("[*] Ready. Post a Moments — the call stack will reveal encryption functions.");
