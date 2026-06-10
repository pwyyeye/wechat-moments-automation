/**
 * 从 mmtls 字符串地址反向找加密函数
 *
 * 策略:
 *   1. 扫描所有 mmtls 字符串及其地址
 *   2. 检查代码段中哪些指令引用了这些地址 (LEA/MOV 等)
 *   3. 找到引用函数，Hook 并 dump 参数
 */

var weixin = Process.findModuleByName("Weixin.dll");
if (!weixin) throw new Error("Weixin.dll not loaded");

console.log("[+] Weixin: base=" + weixin.base + " size=" + (weixin.size/1024/1024).toFixed(0) + "MB");

// Step 1: 找所有 "mmtls" 字符串
var mmtlsRefs = [];
var pattern = "6d 6d 74 6c 73"; // "mmtls"

try {
    Memory.scan(weixin.base, weixin.size, pattern, {
        onMatch: function(addr, size) {
            mmtlsRefs.push(addr);
        },
        onComplete: function() {}
    });
} catch(e) {
    console.log("[!] Memory.scan failed: " + e);
}

console.log("[*] Found " + mmtlsRefs.length + " mmtls refs in Weixin.dll");

// Step 2: 对每个 mmtls 地址，尝试读取附近的字符串上下文
mmtlsRefs.slice(0, 10).forEach(function(addr, i) {
    try {
        var ctx = addr.readCString(200);
        console.log("  [" + i + "] " + addr + " → \"" + (ctx||"") + "\"");
    } catch(e) {}
});

// Step 3: 关键 — 在 mmtls 字符串附近找函数入口
// 函数序言模式: 48 89 5C 24 (mov [rsp+8], rbx) 或 55 (push rbp)
// 搜索每个 mmtls 引用前 200 字节的代码

var funcs = new Set();
mmtlsRefs.slice(0, 20).forEach(function(ref) {
    // 往前搜索 500 字节找函数序言
    for (var off = -500; off < 0; off += 1) {
        try {
            var probe = ref.add(off);
            var b = probe.readU8();
            // 函数序言: 0x55 (push rbp) 或 0x48 0x89 0x5C 0x24 (mov [rsp+8], rbx)
            var b1 = probe.add(1).readU8();
            if (b === 0x55 && b1 === 0x48) {
                funcs.add(probe);
                break;
            }
            if (b === 0x48 && probe.add(1).readU8() === 0x89 && probe.add(2).readU8() === 0x5c) {
                funcs.add(probe);
                break;
            }
            // 或者: 0x40 0x53 (push rbx — MSVC x64 prologue variant)
            if (b === 0x40 && b1 === 0x53) {
                funcs.add(probe);
                break;
            }
        } catch(e) { break; }
    }
});

console.log("\n[*] Found " + funcs.size + " potential function prologues near mmtls refs");

// Step 4: Hook 这些函数
var hooked = 0;
funcs.forEach(function(addr) {
    try {
        var offset = addr.sub(weixin.base);
        Interceptor.attach(addr, {
            onEnter: function(args) {
                console.log("\n[mmtls_func@+" + offset.toString(16) + "] called");
                // dump arg0 和 arg2 (常用 buffer 参数)
                [args[0], args[2]].forEach(function(arg, idx) {
                    if (!arg || arg.isNull()) return;
                    try {
                        var peek = arg.readByteArray(128);
                        var arr = new Uint8Array(peek);
                        var rd = 0;
                        for (var i = 0; i < 100; i++) { if (arr[i] >= 0x20 && arr[i] < 0x7f) rd++; }
                        if (rd > 5) {
                            console.log("  arg" + idx + " readable=" + rd);
                            console.log(hexdump(arg, {length: 256}));
                        }
                    } catch(ex) {}
                });
            }
        });
        hooked++;
        console.log("  Hooked +" + offset.toString(16));
    } catch(ex) {}
});

console.log("[+] Hooked " + hooked + " mmtls-adjacent functions");
console.log("[*] Post a Moments.");
