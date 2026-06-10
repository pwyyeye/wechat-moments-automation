/**
 * 从 Frame #1 的 arg1 指针数组出发，追踪堆对象找到完整的序列化 protobuf
 */

var base = Process.findModuleByName("Weixin.dll").base;
var MAX_DEPTH = 3;
var totalCount = 0;

Interceptor.attach(base.add(0x5c0a01b), {
    onEnter: function(args) {
        if (totalCount >= 2) return;
        var ptrArray = args[2]; // arg1 在 backtrace 中是 r8 (第3个参数)
        if (!ptrArray || ptrArray.isNull()) return;

        totalCount++;
        console.log("\n=== TRACE #" + totalCount + " ptrArray=" + ptrArray + " ===");

        // 指针数组大小: 从 [rsp+0x30] 推断
        var estCount = 20;
        for (var i = 0; i < estCount; i++) {
            var ptr = ptrArray.add(i * 8).readPointer();
            if (ptr.isNull()) break;

            console.log("\n  ptr[" + i + "] = " + ptr);

            // 尝试读取指针指向的数据
            for (var off = 0; off <= 0x20; off += 0x10) {
                try {
                    var start = ptr.add(off);
                    var peek = start.readByteArray(256);
                    var arr = new Uint8Array(peek);
                    var readable = 0;
                    for (var j = 0; j < 150; j++) {
                        var b = arr[j];
                        if ((b >= 0x20 && b < 0x7f) || b === 0x0a || b === 0x12)
                            readable++;
                    }

                    if (readable > 10) {
                        // 找到更多数据
                        var end = arr.length;
                        for (var j = 0; j < arr.length; j++) {
                            if (arr[j] === 0x00) {
                                var z = 0;
                                for (var k = j; k < arr.length && arr[k] === 0x00; k++) z++;
                                if (z > 8) { end = j; break; }
                            }
                        }
                        console.log("    [+" + off.toString(16) + "] readable=" + readable + " len=" + end);
                        console.log(hexdump(start, { length: Math.min(end + 32, 512) }));
                        break; // 找到数据就停止搜索这个指针的偏移
                    }
                } catch(e) {}
            }
        }
    }
});

console.log("[+] Following pointers from Frame #1. Post a Moments.");
