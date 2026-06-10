/**
 * Hook BCryptEncrypt — mmtls 加密必然调用 Windows CNG
 * 在这里可以拦截到加密前的明文数据
 */

var bcrypt = Process.findModuleByName("bcrypt.dll") || Process.findModuleByName("bcryptprimitives.dll");
if (!bcrypt) throw new Error("bcrypt not loaded");

console.log("[+] bcrypt: " + bcrypt.name + " base=" + bcrypt.base);

var exp = null;
bcrypt.enumerateExports().filter(function(e){return e.type==='function'&&e.name==='BCryptEncrypt'}).forEach(function(e){exp=e;});
if (!exp) throw new Error("BCryptEncrypt not found");

var count = 0;
Interceptor.attach(exp.address, {
    onEnter: function(args) {
        // BCryptEncrypt(hKey, pbInput, cbInput, pPaddingInfo, pbIV, cbIV,
        //               pbOutput, cbOutput, pcbResult, dwFlags)
        var pbInput = args[1];
        var cbInput = args[2].toInt32();
        var pbOutput = args[6];
        var cbOutput = args[7].toInt32();

        // 只记录大 buffer（朋友圈数据量级）
        if (cbInput > 1000 && cbInput < 50000 && !pbInput.isNull()) {
            count++;
            console.log("\n=== BCryptEncrypt #" + count + " inputSize=" + cbInput + " ===");

            // dump 明文输入
            try {
                var data = pbInput.readByteArray(Math.min(cbInput, 4096));
                var arr = new Uint8Array(data);

                // 检查是否像 protobuf (有 varint 模式)
                var protoScore = 0;
                for (var i = 0; i < Math.min(arr.length, 200); i++) {
                    var b = arr[i];
                    if (b === 0x0a || b === 0x12 || b === 0x1a) protoScore++;
                    if ((b & 0x07) === 0x00 && b > 8) protoScore++;
                    if ((b & 0x07) === 0x02 && b > 16) protoScore++;
                }

                var readable = 0;
                for (var i = 0; i < Math.min(arr.length, 200); i++) {
                    var b = arr[i];
                    if ((b >= 0x20 && b < 0x7f) || b === 0x0a || b === 0x12) readable++;
                }

                console.log("  protoScore=" + protoScore + " readable=" + readable);
                console.log(hexdump(pbInput, {length: Math.min(cbInput, 2048)}));

                if (protoScore > 10 || readable > 30) {
                    console.log("\n*** POSSIBLE PROTOBUF FOUND ***");
                    // Dump all of it
                    console.log(hexdump(pbInput, {length: cbInput}));
                }
            } catch(e) {
                console.log("  Error: " + e);
            }
        }
    }
});

console.log("[+] Hooked BCryptEncrypt at " + exp.address);
console.log("[*] Post a Moments. Watching for ~14KB protobuf plaintext.");
