var weixin = Process.findModuleByName("Weixin.dll");
if (!weixin) throw new Error("Weixin.dll not loaded");

var addr = weixin.base.add(0x48d29d1);
var count = 0;

Interceptor.attach(addr, {
    onEnter: function(args) {
        if (count >= 2) return;
        count++;

        var buf = args[0];
        if (!buf || buf.isNull()) return;

        var pbStart = buf.add(0x10);
        try {
            var data = pbStart.readByteArray(16384);
            var arr = new Uint8Array(data);
            var end = arr.length;
            var z = 0;
            for (var i = 0; i < arr.length; i++) {
                if (arr[i] === 0x00) { z++; if (z > 16 && i > 50) { end = i - z; break; } }
                else { z = 0; }
            }
            // dump 全部数据到控制台
            console.log("FILE_START::capture" + count + "::" + end);
            console.log(hexdump(pbStart, { length: end + 32 }));
            console.log("FILE_END::capture" + count);
        } catch(e) {}
    }
});

console.log("[+] Ready. Post a Moments.");
