/**
 * Hook Weixin.dll 的 13 个导出函数
 *
 * 关键目标: StartCronetUploadRequest — 可能处理朋友圈上传
 *
 * 使用: frida -p <PID> -l hook_cronet.js
 */

var weixin = Process.findModuleByName("Weixin.dll");
if (!weixin) throw new Error("Weixin.dll not loaded");

console.log("[+] Weixin.dll: " + weixin.base);

var exports = weixin.enumerateExports().filter(function(e) { return e.type === 'function'; });
console.log("[*] Weixin.dll has " + exports.length + " exports:\n");

exports.forEach(function(e) {
    console.log("    " + e.name + " @ " + e.address);

    // Hook 所有 Cronet 相关函数
    if (e.name.indexOf('Cronet') >= 0 || e.name.indexOf('Upload') >= 0) {
        try {
            var funcName = e.name;
            var addr = e.address;
            Interceptor.attach(addr, {
                onEnter: function(args) {
                    console.log("\n=== [" + funcName + "] called ===");
                    // Dump 所有参数
                    for (var i = 0; i < 4; i++) {
                        var arg = args[i];
                        console.log("  arg" + i + ": " + arg);
                        if (arg && !arg.isNull()) {
                            try {
                                // 尝试读取为 string
                                var cstr = arg.readCString(200);
                                if (cstr && cstr.length > 0 && cstr.length < 200) {
                                    console.log("    -> string: " + cstr);
                                }
                            } catch(e) {}
                            try {
                                // 尝试读取为结构体 + hexdump
                                console.log("    -> hexdump:");
                                console.log(hexdump(arg, {length: 512}));
                            } catch(e) {}
                        }
                    }
                    console.log("=== [" + funcName + "] end ===\n");
                }
            });
            console.log("  [HOOKED]");
        } catch(ex) {
            console.log("  [FAILED: " + ex + "]");
        }
    }

    // Hook SetWeixinCallbackFunc — 看看设置了什么回调
    if (e.name === 'SetWeixinCallbackFunc') {
        try {
            Interceptor.attach(e.address, {
                onEnter: function(args) {
                    console.log("\n[SetWeixinCallbackFunc] callback=" + args[0]);
                }
            });
            console.log("  [HOOKED]");
        } catch(ex) {}
    }
});

console.log("\n[*] Ready. Post a Moments to capture Cronet calls.");
