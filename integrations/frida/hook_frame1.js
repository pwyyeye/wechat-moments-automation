var weixin = Process.findModuleByName("Weixin.dll");
if (!weixin) throw new Error();

// Hook Frame #1 — 更接近 socket，可能拿到序列化后的 protobuf bytes
Interceptor.attach(weixin.base.add(0x5c0a01b), {
    onEnter: function(args) {
        [args[0], args[2]].forEach(function(reg, idx) {
            if (!reg || reg.isNull()) return;
            try {
                var peek = reg.readByteArray(256);
                var arr = new Uint8Array(peek);
                var readable = 0;
                for (var i = 0; i < 100; i++) {
                    var b = arr[i]; if ((b >= 0x20 && b < 0x7f) || b === 0x0a || b === 0x12) readable++;
                }
                if (readable > 5) {
                    var end = arr.length;
                    for (var i = 0; i < arr.length; i++) {
                        if (arr[i] === 0x00) { var z = 0; for (var j = i; j < arr.length && arr[j] === 0x00; j++) z++;
                            if (z > 8) { end = i; break; } }
                    }
                    console.log("\n[Frame#1 arg" + idx + "] " + end + " bytes");
                    console.log(hexdump(reg, {length: Math.min(end + 16, 2048)}));
                }
            } catch(e) {}
        });
    }
});
console.log("[+] Hooked Frame #1. Post.");
