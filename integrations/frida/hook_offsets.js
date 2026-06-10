/**
 * Hook 关键偏移 — 在调用栈帧位置 dump 参数找明文 protobuf
 *
 * 从上一步 backtrace 得到的关键偏移:
 *   Frame #3: Weixin.dll+0x48d29d1 ← 最接近加密
 *   Frame #2: Weixin.dll+0x48d8f17
 *
 * 使用: frida -p <PID> -l hook_offsets.js
 */

var weixin = Process.findModuleByName("Weixin.dll");
if (!weixin) throw new Error("Weixin.dll not loaded");

var base = weixin.base;
console.log("[+] Weixin.dll base: " + base);

// 关键偏移（从 backtrace 获得）
var offsets = [
    { offset: 0x48d29d1, label: "Frame#3 (deepest — near encrypt)" },
    { offset: 0x48d8f17, label: "Frame#2 (caller)" },
    { offset: 0x5c0a01b, label: "Frame#1 (mmcronet)" },
    { offset: 0x4827f9b, label: "Frame#0 (near socket)" },
];

var hooked = 0;

offsets.forEach(function(o) {
    var addr = base.add(o.offset);
    try {
        Interceptor.attach(addr, {
            onEnter: function(args) {
                console.log("\n[" + o.label + "] Weixin+0x" + o.offset.toString(16));

                // Dump 所有寄存器和参数
                var ctx = this.context;
                console.log("  rcx=" + ctx.rcx + " rdx=" + ctx.rdx + " r8=" + ctx.r8 + " r9=" + ctx.r9);

                // 尝试 dump 栈上的参数 (x64 调用约定: 前4个参数在寄存器，之后的在栈)
                var sp = ctx.rsp;
                for (var i = 0; i < 6; i++) {
                    var p = sp.add(0x28 + i * 8).readPointer();
                    console.log("  [rsp+0x" + (0x28 + i*8).toString(16) + "] = " + p);
                }

                // 对每个可能的 buffer 参数尝试 hexdump
                [ctx.rcx, ctx.rdx, ctx.r8, ctx.r9].forEach(function(reg, idx) {
                    if (reg && !reg.isNull()) {
                        try {
                            // 尝试将其作为指向缓冲区的指针
                            var peek = reg.readByteArray(128);
                            if (peek) {
                                var arr = new Uint8Array(peek);
                                // 检查是否像结构化数据（非纯二进制）
                                var readable = 0;
                                var binary = 0;
                                for (var j = 0; j < Math.min(arr.length, 64); j++) {
                                    var b = arr[j];
                                    if (b >= 0x20 && b < 0x7f) readable++;
                                    if (b < 0x08 && b > 0x00) binary++;
                                }

                                if (readable > 10 || (binary > 0 && readable > 5)) {
                                    console.log("  arg" + idx + " (" + readable + " readable/" + binary +
                                               " binary bytes) — possible protobuf:");
                                    console.log(hexdump(reg, {
                                        length: Math.min(2048, readable + binary + 100)
                                    }));
                                }
                            }
                        } catch(ex) {}
                    }
                });
            }
        });
        hooked++;
        console.log("[+] Hooked " + o.label + " at " + addr);
    } catch(ex) {
        console.log("[-] Failed: " + o.label + ": " + ex);
    }
});

console.log("[+] Hooked " + hooked + "/" + offsets.length + " offsets");
console.log("[*] Post a Moments to dump encryption arguments.");
