# OKX Orbit — Báo cáo bổ sung (phiên REA MCP từ repo REA)

**Ngày:** 2026-11-05  
**Artifact:** `com.okinc.okex.gp` 6.194.0 (versionCode 737202), APK SHA-256 `150f7883ea2d1ea762638b6cb5628cf63397d4fe1d756c9a6eb00252bfc90b89`  
**Công cụ:** REA 6.8.0 chạy từ chính repo `rea-main`; MCP `rea` + JADX headless bridge v10 / engine 0.7.1; Java 26; heap 8 GiB; `metadata_scope=parsed_members`  
**Tài liệu gốc:** [OKX_ORBIT_REVERSE_ENGINEERING_AND_CODEBASE_AUDIT_2026-11-05.md](./OKX_ORBIT_REVERSE_ENGINEERING_AND_CODEBASE_AUDIT_2026-11-05.md)

Báo cáo này **bổ sung và đính chính** báo cáo gốc. Không thay thế báo cáo gốc. Mọi kết luận mới đều có Evidence ID từ MCP hoặc trích dẫn file/hàng trong cây decompile.

> **Ràng buộc an toàn giữ nguyên:** không đọc, dùng, sao chép hay exfiltrate cookie/token phiên OKX/Binance trong `okx.txt`, `bi.txt`, `cookies/`. Không gửi dữ liệu phân tích ra ngoài. Chỉ phân tích tĩnh/local; mọi kiểm thử live phải nằm trong phạm vi bug bounty được uỷ quyền.

---

## 0. TL;DR — có gì mới

1. **Đính chính:** `headers-order-string-hash` là **MD5** (lowercase hex, UTF-8) của tên header đã sort lowercase, join không separator — **không phải SHA-256** như báo cáo gốc.
2. **Planet publish không cài `OKRiskSessionInterceptor`**: `PlanetDependencyContainer.ActionBar extends EKm` và chỉ override `initClientBuilder()` gọi `super`. Tuy nhiên `DeviceInfoInterceptor` (global) gọi risk callback `C5840Jei`, và callback này **có** thêm `tmx-session-id`, `tmx-session-status`, `headers-order-string-hash` cùng `risk-params`, `fingerprint-id`, `X-FpToken*`, `x-sec-token`. Vì vậy capture có tmx/hash trên publish là **hợp lý**, nhưng đường đi khác báo cáo gốc.
3. **Serialization của Planet đã xác định:** kotlinx-serialization JSON với `encodeDefaults=false`, `ignoreUnknownKeys=true`, `lenient=true`, `useArrayPolymorphism=true`; converter order `[EMH(kotlinx), kotlinx, EMH(gson), gson]`; request body do kotlinx xử lý. Hệ quả: các field optional có giá trị mặc định (`null`, list rỗng, `""`) **bị bỏ khỏi JSON**.
4. **Khóa R1 là Android Keystore, không export được:** alias `JwtTokenSecureKey`, EC, purpose `SIGN|VERIFY` (12), digest SHA-256; sinh tự động lần đầu nếu chưa có. `X-Client-Public-Key` = `Base64_NO_WRAP(X.509 SPKI)`. Private key không thể trích xuất từ APK.
5. **Điều kiện ký R1:** mọi `POST` mặc định được ký (trừ tag `WEB3` hoặc header `OKAsymmetricSignEnable: "false"`); `GET` chỉ ký khi `OKAsymmetricSignEnable: "true"`. Signature `SHA256withECDSA`, DER, Base64 NO_WRAP. Header gửi đi: `X-Request-Timestamp` (ms), `X-Client-Signature`, `X-Client-Public-Key`; sau đó các header debug/key bị remove.
6. **Cổng mã hoá chính xác:** chỉ khi `Encrypt-Strategy: TIME` **và** POST; `appTime` được chèn trước khi ký nếu header `Encrypt-ignore-extra-body-param != 1`. `OKEncryptInterceptor` (EMe) chạy **sau** `AsymmetricSignInterceptor`, thay body bằng `{"body":"..."}` và remove `Encrypt-Strategy`. Publish 6.194.0 không set header này → nhánh mã hoá là no-op.
7. **Thứ tự interceptor chính xác** (application/network/post) đã có đủ; báo cáo gốc xếp sai vị trí network interceptors và post interceptors.
8. **DTO chi tiết:** `group` luôn được serialize kể cả `""`; `contentTags` bị bỏ nếu rỗng; ảnh chỉ gửi `url/width/height/fileSize` (không hardcode 1024); `HashTag.type` lấy từ `topicType`, deeplink topic chỉ khi `type == 4`, mention có deeplink `planet/personal?authorId=...`.

---

## 1. Phiên làm việc và artifact

### 1.1. MCP chạy bằng repo REA

Phiên này dùng đúng MCP server `rea` được build từ repo `rea-main` (không dùng công cụ ngoài), với engine JADX headless 0.7.1. Do APK đầy đủ 566.770.549 byte gây OOM cho JADX, tiếp tục dùng kỹ thuật **mini-APK theo DEX**:

| Mini APK | DEX | Kích thước | SHA-256 | Mục đích |
|---|---|---:|---|---|
| `work/apk-mini/okx-6.194.0-classes6.apk` | classes6 | 3.008.186 | `1d8c35d9c16793c4ee1c9427c34a978df74ca14e2a637a32f66a4717e5a9ee3c` | signing/network interceptors |
| `work/apk-mini/okx-6.194.0-classes14.apk` | classes14 | 3.216.393 | (đã dùng phiên trước) | MD5 helper |
| `work/apk-mini/okx-6.194.0-classes19.apk` | classes19 | 3.003.195 | (đã dùng phiên trước) | EKm/EKj/EMe |
| `work/apk-mini/okx-6.194.0-classes21-jni.apk` | classes21 + `libOKCore.so` | 254.699.938 | `215fda9f3d19dfb5e3a6daaadcea5518aad830dc67c559bef0093100fdb076f2` | NativeApi/JNI |
| `work/apk-mini/okx-6.194.0-classes22.apk` | classes22 | 3.226.837 | `c19a1ad786c7e6cf4573cc22a34b7d54f4a7c139d12ac0fe8570c8c1dedf1697` | OKRiskSessionInterceptor, hash utils |
| **`work/apk-mini/okx-6.194.0-classes23.apk`** | **classes23** | **25.260.328** | **`35c13a33e215c557f9f6ebc3841f25a518b0ec85bd40a3004e9cee1304be75f5`** | **mới: `o.KsL`, `o.KsO` — nguồn khoá R1** |
| `work/apk-mini/okx-6.194.0-classes25.apk` | classes25 | 3.193.467 | (đã dùng phiên trước) | Planet repository/DTO |
| `work/apk-mini/okx-6.194.0-classes31.apk` | classes31 | 2.917.625 | `b662c0e51bd7a2ac040ab5e7e6ba8bd14c768d7251fcc46d8cb1c436adaa4b55` | `o.acaM` (group) |

Mini APK classes23 được tạo trong phiên này bằng cách lấy `AndroidManifest.xml` + `resources.arsc` + `classes23.dex` (đổi tên thành `classes.dex`) từ APK gốc. Xác định DEX chứa class bằng parser `class_defs` tự viết (string_ids/type_ids), vì tìm theo tên file nguồn có thể nhầm do Windows không phân biệt hoa/thường.

### 1.2. Evidence IDs chính của phiên này

| Nội dung | Evidence ID |
|---|---|
| `o.KsL` class summary (classes23) | `ev_6e8b220ccce8e7bd589d7698e673bf5b23cf0c4c4eb4d104b1eb1d52c5d90584` |
| `KsL.copydefault()` → `KeyGenParameterSpec("JwtTokenSecureKey", 12)` | `ev_b283975457e5ce9ea54d11c5a5d21a6f0056fcb4e5591854119a34eaa00addc1` |
| `KsL.KWHzl()` → `KsO.getKeyPair("JwtTokenSecureKey")` | `ev_39eb20f64793b75126305fbc53654ac389197218f22e18a852c7c948e5826ac9` |
| `KsL.EZpvd()` → public key Base64 SPKI | `ev_4594cac76d63745b8a120f4c7130d7c19a667018eaae2e5e5081b63afdbdf7c6` |
| `KsL.AEQbTJ()` → PrivateKey | `ev_52498ddbf70361b60bec247c35bbd1df419dae0ed132cfb5e2d78a13dead244c` |
| `KsL.KWHzl(byte[])` → `Base64.encodeToString(b, 2)` | `ev_a2e0cd93f7ad92cb1dd55fcc41e43dd669f3ddfe025ab92efa7367c1444ba82c` |
| `KsL.AhwBna()` → `containsAlias("JwtTokenSecureKey")` | `ev_a3874de993b9b7a8d79d5c8ab3d42315e60f90ad329c11ef66d2b0a733e3294e` |
| `KsL.<clinit>` (smali) → load keystore + generate `EC` nếu thiếu | `ev_3d5df3de092d042799c23a2289b4347f7b30b9d1bdb993be143d3cf1d94c84c8` |
| `KsO.OLrzqt(String, KeyGenParameterSpec)` → `KeyPairGenerator` | `ev_8259318367e41d8da9717e66398b6d4a401c57b90455f10918f5fd4a1af78586` |
| `KsO` class summary | `ev_563cc1ea6f0b5df72c401796806c042a218936b0a3adc3cbadc2e6a23a40ea7e` |
| `KsO.getKeyPair$default` | `ev_3b623bfa43a9b6f39db048456ffa5338ab2fe6c9c182854f2e4032a876320e16` |
| `KsL.OLrzqt(KsM)` (gán callback lỗi) | `ev_cf3d6165784bc831d344364e6b975bfd03fc9194902d8443fac9cf8a5455ceef` |
| `KsL.OLrzqt()` (load KeyStore) | `ev_2943ffa08a97cde241a2529ea195e3e993d8f481184dac125cd69c13fd7e61a1` |

Evidence phiên trước vẫn hiệu lực (xem báo cáo gốc): MD5 `o.uIm` `ev_ad01608b8c940d47416f922120f8ee82ac5337ff5f8ebf91721896cb13983da7`; MD5/SHA-256 `o.JkN` `ev_f3134176f0f5edc8b96c225532a1b7bf2c06205b9a715b7efa9dd12615bf88fe` / `ev_0e43ce5e8e86be43befe51d5704b6d8f5102509fa161b1164ad911670b802233`; EKm init `ev_0cf45dd383092c7a0989cdb0981e2dd69eb883f1a3ba5e48895a7857f5da2c34`; EMe gates `ev_afb56e125603cadfee186645baf8bc201668612fe5425968b32e7a5c83877fb7`; EKx `ev_2831d96222806bf304a783cf41f0a9306a57bce205f0a24689a2a1c5211baa9f`; EKj `ev_fc9f91dd6fc477a9650a4e8a8a6a4512a108ba68b5353944b678b3784ebbdff7`; ENg.EZpvd `ev_b3e2984ca058b24ce963ab74bba6f3d943b9a6e99d8b223a780660a1b6278e20`.

---

## 2. Đính chính báo cáo gốc

### 2.1. `headers-order-string-hash` là MD5, không phải SHA-256

**Báo cáo gốc** (mục 4.4 và 4.5) ghi SHA-256. **Sai.** Mã thực tế:

`com/okinc/okrisk/OKRiskSessionInterceptor.java:29`:

```java
return request.newBuilder()
    .header("headers-order-string-hash",
        C6054JkN.OLrzqt.copydefault(
            CollectionsKt.joinToString$default(
                CollectionsKt.copydefault(request.headers().names(), new TaskDescription()),
                "", null, null, 0, null, null, 62, null)))
    .build();
```

`o.C6054JkN` là singleton `HashUtils`; `OLrzqt` là **instance**, không phải method SHA-256:

```java
// C6054JkN.java:60-74
public final String copydefault(@NotNull String str) {
    MessageDigest messageDigest = MessageDigest.getInstance("MD5");
    byte[] digest = messageDigest.digest(str.getBytes(Charsets.UTF_8));
    ...
    for (byte b : digest) {
        str2 = str2 + String.format("%02x", Byte.valueOf(b));
    }
    return str2;
}
```

Công thức đúng:

```python
names = sorted((h.lower() for h in header_names))   # lowercase Locale.ROOT, sort comparator adcr
digest = md5("".join(names).encode("utf-8")).hexdigest()   # lowercase hex, không separator
```

- `C6054JkN` còn có `OLrzqt(String)` dùng SHA-256, nhưng **không** được gọi ở đây.
- Comparator `TaskDescription` lowercase bằng `Locale.ROOT` rồi so sánh qua `o.adcr.AEQbTJ` (không cần biết chi tiết comparator để mô phỏng: sort theo chuỗi lowercase).
- Hash tính trên `request.headers().names()` tại thời điểm interceptor chạy — **không** phải header cuối cùng do OkHttp thêm.

**Ảnh hưởng:** chỉ các client cài `OKRiskSessionInterceptor` (OKRisk config, FIDO2, một số config khác) mới dùng hash này theo đường đó. Planet publish đi đường khác (mục 2.2), nhưng vẫn có thể có hash qua risk callback.

### 2.2. Planet publish: không có `OKRiskSessionInterceptor`, nhưng có risk callback

**Bằng chứng cấu hình Planet:**

`o/C9532PbY.java:66-75`:

```java
public static final class ActionBar extends EKm {
    public ActionBar() {
        super((String) null, 1, (DefaultConstructorMarker) null);
    }
    public OkHttpClient.Builder initClientBuilder() {
        return super.initClientBuilder();
    }
}
```

`o/C9532PbY.java:80-84`: `QVAiDq` (ENe) = `new ActionBar()`; `o/C9532PbY.java:308-310`: `DGUQLI()` trả về ENe này; `o/C9532PbY.java:320-322`: service `InterfaceC9578PcS` được tạo bằng `ENg.EZpvd(adfG.KWHzl(...), copydefault.DGUQLI())`.

`o/C9532PbY.java` không hề cài `OKRiskSessionInterceptor`. Base `EKm.initClientBuilder` cũng không cài. Vì vậy **không có** `tmx-session-*`/`headers-order-string-hash` từ `OKRiskSessionInterceptor` cho publish.

**Nhưng** base chain có `DeviceInfoInterceptor` (global list). `com/okinc/biz/net/common/DeviceInfoInterceptor.java:52-68`:

```java
Function2<Chain, Request.Builder, Unit> callback = C3922EKw.KWHzl.isConnected();
if (callback != null) {
    callback.invoke(chain, builder);   // risk callback
    return;
}
// fallback: apply C3922EKw.zLjUOn() (risk-params, fingerprint-id)
```

Callback được đăng ký bởi OKRisk SDK (`o/C5840Jei`). `C5840Jei.copydefault` thêm:

- `risk-params`, `fingerprint-id` (dòng 64-65);
- `X-FpToken`, `X-FpToken-Signature`, `X-FpToken-Timestamp` nếu có token và POST (dòng 69-75);
- `tmx-session-id`, `tmx-session-status`, `headers-order-string-hash` (dòng 206-214);
- `x-sec-token` nếu path thuộc whitelist (dòng 77-100).

**Kết luận đúng:** Planet publish **có thể** mang tmx/hash/risk headers qua `DeviceInfoInterceptor → C5840Jei`, tuỳ việc risk callback đã được đăng ký hay chưa; nếu chưa, app dùng fallback `risk-params=fp-status=-2`, `fingerprint-id=devId`. Capture có tmx/hash trên publish là **không mâu thuẫn** với việc Planet không cài `OKRiskSessionInterceptor`.

**Khác biệt quan trọng về snapshot hash:**

| Đường | Snapshot đầu vào của hash |
|---|---|
| `OKRiskSessionInterceptor.copydefault` | `request.headers().names()` sau khi đã thêm `tmx-session-*` (request đầu vào của interceptor) |
| `C5840Jei.EZpvd` (gọi từ `DeviceInfoInterceptor`) | `chain.request().headers().names()` — **trước** khi `DeviceInfoInterceptor` thêm `User-Agent`, `devId`, `platform`, `real-app-version`, `x-utc`, `lua-version`, `app_web_mode`, dynamic map |

Vì vậy cùng một công thức MD5 nhưng tập header khác nhau. Không được hardcode; phải log đúng stage.

### 2.3. Thứ tự interceptor chính xác

Báo cáo gốc xếp "network interceptors" ở vị trí 13 và "post interceptors" ở 17, đồng thời gộp `C18977eIZ/EMC/C19038eJm` vào network. Thực tế `o/C3979ENk.java:57-73` + `o/C3913EKm.java:114-181`:

**Application interceptors (theo thứ tự add):**

1. `EMS(tag)` — chỉ khi `getRequestHeaderTag() != NONE`. Planet `ActionBar` không override → `NONE`, nên **không có EMS**.
2. `buildPreCustomInterceptors()` → `adbF.EZpvd()`.
3. Global list `C3910EKj.OLrzqt().OLrzqt()` (`o/C3922EKw.java:477-499`), thứ tự:
   1. `C19026eJa` (DebugUrlInterceptor, có điều kiện `z || vzX...`)
   2. `C19029eJd` (nếu `QbewEr()`)
   3. `C19034eJi`
   4. `ELL(DohTypeEnum.HTTP)`
   5. **`DeviceInfoInterceptor`**
   6. **`EntityInterceptor`**
   7. `C19043eJr`
   8. `TimeOutSettingInterceptor`
   9. `C19028eJc`
   10. `C19037eJl`
   11. `AuthorizationInterceptor`
   12. `RenewTokenInterceptor`
   13. `RefererInterceptor`
4. `C3912EKl`
5. `EMY` — nếu `QOLQEE()`
6. `C19036eJk`
7. `C3954EMg`
8. `C19040eJo`
9. `OKSignInterceptor` (legacy)
10. **`AsymmetricSignInterceptor`**
11. **`XIDInterceptor`**
12. **`OKDecryptInterceptor`**
13. `C18977eIZ` (application)
14. `EMC` (application)
15. `C19038eJm` (application)
16. List1 `C3910EKj.OLrzqt().EZpvd()` (application)
17. **Post interceptors** do `ENk.createService` gọi `addPostInterceptor()` (`o/C3979ENk.java:53-72,109-119`) → `EMe` (`OKEncryptInterceptor`) và `EMP`, append cuối nếu chưa có cùng class.

**Network interceptors:** list3 `C3910EKj.OLrzqt().AEQbTJ()` được add bằng `addNetworkInterceptor` (`o/C3913EKm.java:140-143`), chạy sau toàn bộ application interceptor.

**Hệ quả:** `AsymmetricSignInterceptor` chạy trước `EMe`; nếu request có `Encrypt-Strategy: TIME`, chữ ký phủ **plaintext** (đã chèn `appTime`), sau đó EMe mới mã hoá body.

### 2.4. Serialization JSON của Planet (giải quyết mục 6.7 báo cáo gốc)

Báo cáo gốc để mở câu hỏi "JSON converter của Planet có encodeDefaults hay không". Đã xác định:

`o/C3913EKm.java:53`:

```java
adbF.copydefault(new Converter.Factory[]{
    C12395aho.copydefault(C3918EKr.OLrzqt(), C3918EKr.AEQbTJ()),
    GsonConverterFactory.create()
});
```

`o/C3918EKr.java:14-32`:

```java
public static final Json KWHzl = JsonKt.Json$default(null, new Function1() {
    public Object invoke(Object obj) {
        JsonBuilder b = (JsonBuilder) obj;
        b.setEncodeDefaults(false);
        b.setIgnoreUnknownKeys(true);
        b.setLenient(true);
        b.setUseArrayPolymorphism(true);
        return Unit.INSTANCE;
    }
}, 1, null);
```

- `C12395aho.copydefault(StringFormat, MediaType)` tạo `C12394ahn(application/json, Serializer.Application(json))` — Retrofit converter của kotlinx-serialization.
- `EKm.proceedConvertFactories` (`o/C3913EKm.java:193-201`) trả về `[EMH(kotlinx), kotlinx, EMH(gson), gson]`.
- `EMH` **chỉ** override `responseBodyConverter` (bọc `EME`), **không** override `requestBodyConverter`. Do đó request body của `@Serializable` DTO do **kotlinx** serializer xử lý; Gson chỉ là fallback.
- `C3980ENl` bọc từng factory và nuốt exception → null, không đổi hành vi request.

**Kết quả:** JSON body của publish dùng:

```text
encodeDefaults = false
ignoreUnknownKeys = true
lenient = true
useArrayPolymorphism = true
Content-Type: application/json
```

**Hệ quả trực tiếp:** field optional có giá trị bằng default bị **omit**. Điều này giải thích vì sao body capture thường không có `draftId`, `quotedContentId`, `activityCard`, `channelData`, `scheduledAt`, `contentId`, và không có `imageList`/`pluginList`/`contentTags` khi rỗng.

### 2.5. Nguồn khoá R1: Android Keystore (không export)

Báo cáo gốc chỉ nói "R1 dùng keypair của app". Bổ sung chính xác:

`com/okinc/common/application/BaseApplication.java:5388-5389`:

```java
KsL ksL = KsL.copydefault;
C19032eJg.copydefault.AEQbTJ(new VoiceInteractor(
    new C19030eJe(ksL.EZpvd(), ksL.AEQbTJ(), null, 4, null)));
```

`KsL` (`o.KsL`, **classes23.dex**) là `SecureKeyUtils`:

- `<clinit>` (Evidence `ev_3d5df3de...`):
  - load `AndroidKeyStore` qua `KsO.OLrzqt()`;
  - `AhwBna()` kiểm tra `KeyStore.containsAlias("JwtTokenSecureKey")`;
  - nếu chưa có: `KsO.OLrzqt("EC", copydefault())` → `KeyPairGenerator.getInstance("EC", "AndroidKeyStore").initialize(spec).generateKeyPair()`.
- `copydefault()`: `new KeyGenParameterSpec.Builder("JwtTokenSecureKey", 12).setDigests("SHA-256").build()`.
  - `12 = PURPOSE_SIGN (4) | PURPOSE_VERIFY (8)`.
- `EZpvd()`: lấy certificate → `PublicKey.getEncoded()` (X.509 SubjectPublicKeyInfo DER) → `Base64.encodeToString(bytes, 2)` (NO_WRAP).
- `AEQbTJ()`: trả `PrivateKeyEntry.getPrivateKey()`.
- `KWHzl()`: `KsO.getKeyPair$default(KsO.AEQbTJ, "JwtTokenSecureKey", null, 2, null)`.

**Hệ quả:**

- Private key R1 nằm trong Android Keystore (thường hardware-backed), **không thể trích xuất** từ APK hay từ thiết bị không root/bypass.
- `X-Client-Public-Key` gửi kèm mọi request đã ký là public key SPKI của khoá này.
- Nếu codebase Python dùng private key lấy từ capture/thiết bị khác, cần đảm bảo public key tương ứng được server chấp nhận/bind với phiên. Đây là **ràng buộc vận hành**, không phải lỗi codebase.
- `AsymmetricSignInterceptor.copydefault(Request)` còn có nhánh override bằng header `X-Client-Public-Key` + `X-Client-Private-Key` (debug/test), và nhánh `X-Alias-Key`; cả hai header bị remove trước khi gửi (`o/.../AsymmetricSignInterceptor.java:182-195,238-243`).

### 2.6. Điều kiện ký và header

`com/okinc/biz/net/common/AsymmetricSignInterceptor.java:197-211`:

```text
sign = header("OKAsymmetricSignEnable") == "true"
       OR (requestTag != WEB3 AND header != "false" AND method == "POST")
```

- Planet `ActionBar` dùng `RequestTagEnum.NONE` (không override `getRequestHeaderTag()`), nên mọi POST publish đều được ký.
- GET chỉ ký khi có `OKAsymmetricSignEnable: true`.
- Không có nơi nào trong app 6.194.0 set `X-Sign-Type-Key` (chỉ đọc ở dòng 75 và remove ở dòng 239) → publish luôn đi nhánh **R1**.

`AsymmetricSignInterceptor.intercept` (dòng 107-112):

```java
AEQbTJ(builderNewBuilder); // remove X-Sign-Type-Key, X-Alias-Key, X-Client-Public-Key, X-Client-Private-Key
if (!sig.isEmpty() && keyBean != null) {
    builder.header("X-Request-Timestamp", String.valueOf(nowMs));
    builder.header("X-Client-Signature", sig);
    builder.header("X-Client-Public-Key", keyBean.publicKey());
}
```

- `sig = Base64_NO_WRAP(DER(SHA256withECDSA(privateKey, UTF8(payload))))` (`o/C19032eJg.java:57-72`).
- Payload: POST `encodedPath + body + timestampMs`; GET `encodedPath + encodedQuery + timestampMs`.
- MultipartBody **không ký** (`AsymmetricSignInterceptor.java:128-131`).
- Nếu `keyBean == null` hoặc sig rỗng → **không gửi** 3 header signing (nhưng vẫn gửi request).

### 2.7. `appTime` và mã hoá — chính xác từng bước

`o/C3952EMe.java`:

- Gate (`StateListAnimator.AEQbTJ`): `header("Encrypt-Strategy") == "TIME"` **và** `method == POST`.
- Ignore (`StateListAnimator.OLrzqt`): `1 == uAv.djBIcL(header("Encrypt-ignore-extra-body-param"))`.
- Trong `AsymmetricSignInterceptor.AEQbTJ(Request, Builder, timestamp)` (dòng 140-180):
  - Nếu gate false **hoặc** ignore true → trả body nguyên.
  - Nếu body null → `{"appTime": "<timestampMs>"}`.
  - Nếu content-type subtype `json` → `JSONObject.put("appTime", String.valueOf(timestampMs))`.
  - Nếu `FormBody` → append `&appTime=<timestampMs>`.
- Trong `EMe.intercept` (post interceptor, chạy sau ký):
  - body null + không ignore → `{"appTime": "<nowMs>"}`;
  - lấy server time (nếu có) hoặc `nowSec`;
  - `NativeApi.encryptBodyOptimized(body, timestampSec, signToken)` → `{"body":"<encrypted>"}`; remove `Encrypt-Strategy`;
  - nếu response `code == 400100` → retry một lần với header `X-Client-Time = NativeApi.encryptContent(signToken)`.

Publish 6.194.0 **không** set `Encrypt-Strategy`, nên toàn bộ nhánh trên không chạy. Chữ ký phủ đúng bytes JSON gửi đi.

### 2.8. Legacy `OKSignInterceptor`

Vẫn như báo cáo gốc: chỉ ký khi request có `OK-VERIFY-TOKEN`; GET payload `encodedPath + encodedQuery`; POST `encodedPath + body`; dùng `NativeApi.netApiSignature`. Publish không gửi `OK-VERIFY-TOKEN` → không dùng legacy sign.

---

## 3. DTO và payload — chi tiết bổ sung

### 3.1. `PublishRequest` — field nào luôn có, field nào bị omit

Constructor và serializer: `com/okinc/planet/domain/remote/dto/PublishRequest.java:67-167`.
Required bitmask `1289` = bit 0, 3, 8, 10. Với `encodeDefaults=false`, generated serializer kiểm tra từng field:

| # | Field | Default | JSON khi default | Ghi chú |
|---:|---|---|---|---|
| 0 | `publishId` | — | **luôn có** | `String.valueOf(System.currentTimeMillis())` |
| 1 | `draftId` | `null` | omit | chỉ draft |
| 2 | `quotedContentId` | `null` | omit | quote |
| 3 | `content` | — | **luôn có** | nội dung cuối |
| 4 | `imageList` | `[]` | omit nếu rỗng | list `PublishImageItem` |
| 5 | `pluginList` | `[]` | omit nếu rỗng | list `PluginData` |
| 6 | `contentTags` | `ContentTags([],[])` | omit nếu rỗng | |
| 7 | `activityCard` | `null` | omit | |
| 8 | `channelId` | — | **luôn có** | enum value |
| 9 | `channelData` | `null` | omit | map |
| 10 | `group` | — | **luôn có, kể cả `""`** | serializer gọi `encodeStringElement` trực tiếp |
| 11 | `scheduledAt` | `null` | omit | Long; đơn vị chưa xác nhận trong subset decompile |
| 12 | `contentId` | `null` | omit | |

**Lưu ý:** khác với các field optional, `group` **không** có default và **luôn** được encode, nên body tối thiểu có `publishId`, `content`, `channelId`, `group` (kể cả `group: ""`).

### 3.2. `ContentTags`, `HashTag`, `CashTag`

`ContentTags` (`.../dto/ContentTags.java:100-106`):

- `cashTagList` omit nếu rỗng.
- `hashTagList` omit nếu rỗng.
- Nếu cả hai rỗng → `contentTags` bị omit khỏi `PublishRequest`.

`HashTag` (`.../dto/HashTag.java:118-133`) — JSON key đúng theo tên property:

| Key | Kiểu | Bắt buộc | Ghi chú |
|---|---|---|---|
| `tag` | String | có | ví dụ `"#topic "`, `"$BTC "`, `"@name "` |
| `activityKey` | String | có | topic = `topicId`; link/mention = `""` |
| `position` | List\<Integer\> | có | 1-based rank |
| `linkUrl` | String? | omit nếu null | link |
| `deeplinkUrl` | String? | omit nếu null | topic type 4, mention |
| `type` | Integer? | omit nếu null | topic = `topicType`; link = 1; mention = 2 |
| `authorId` | String? | omit nếu null | mention |

`CashTag` (`.../dto/CashTag.java:93-95`): `tag`, `coinName`, `position` — tên field là **`coinName`**, không phải `symbol`.

### 3.3. Mapping tag từ `PublishDataMapper`

`com/okinc/planet/domain/remote/mapper/PublishDataMapper.java:86-118`:

| Loại | Constructor thực tế | Chi tiết |
|---|---|---|
| Topic | `HashTag(tag, topicId, positions, null, deeplink?, topicType, null)` | `tag = C10267Pvu.KWHzl(displayText)`; `deeplink` chỉ khi `topicType == 4`, dạng `orbit/topic/detail?topicId=<enc>&title=<enc>`; `type = topicType` |
| Link | `HashTag(displayText, "", positions, linkUrl, deeplink?, 1, null)` | `linkUrl = LinkTagMeta.linkUrl ?: displayText`; `deeplinkUrl = LinkTagMeta.deeplinkUrl` |
| Mention | `HashTag(formatted, "", positions, null, "planet/personal?authorId=<enc>", 2, authorId)` | mention **có** `deeplinkUrl`; `authorId` lấy từ server |
| Cash | `CashTag(formatted, displayText, positions)` | `CashTag(tag, coinName, position)` |

- `C10267Pvu` thêm hậu tố space: `#x `, `$SYM `, `@name `.
- `position` = rank 1-based của match cùng loại; pattern yêu cầu `\s` sau tag (xem báo cáo gốc 5.5).
- Không được tự đặt `type = 4` cho mọi topic: `type` là `topicType` server trả về; deeplink chỉ xuất hiện khi type 4.

### 3.4. Ảnh

`PublishDataMapper.OLrzqt(List<ImageItem>)` (`.../PublishDataMapper.java:36-57`):

```java
new PublishImageItem(
    imageItem.getRemoteUrl(),   // url
    null,                       // displayUrl -> default ""
    width  ?: "",
    height ?: "",
    null,                       // srcKey -> default ""
    fileSize ?: ""
);
```

`PublishImageItem` (`.../dto/PublishImageItem.java:100-118,126-134`):

- JSON chỉ gồm `url`, `width`, `height`, `fileSize` khi khác `""`; `displayUrl`/`srcKey` bị omit vì default `""`.
- Không có hardcode `1024`; width/height/fileSize lấy từ `ImageItem` (ảnh đã upload).
- Presign: `POST /priapi/v5/content/image/presign` body `Map<String,String>` (`{requestId}`), sau đó PUT lên URL presign. Nếu presign có ký content-type, upload phải khớp (vẫn là điểm cần xác minh capture).

### 3.5. `PluginData`

`.../dto/PluginData.java:60-69`:

- Luôn có: `type`, `createTime`, `metaData`.
- `deepLink` omit nếu `""`.
- `minVersion` omit nếu `null`.

### 3.6. `publishId`, `group`, `channelId`, quote/schedule

`o/C9613PdB.java:19-59`:

- `publishId = PublishDataMapper.OLrzqt()` = `System.currentTimeMillis().toString()`.
- `group = acaM.OLrzqt.AEQbTJ()`; nếu null → `""` (`PublishRequest` vẫn chứa `group: ""`).
- `effectiveChannelId`: nếu có quote (`quotedContentId` non-empty **hoặc** `QuotedContentDto != null`) → ép `PlanetPublisherChannelId.QUOTE`; ngược lại giữ channel truyền vào.
- `isScheduled = (scheduledAt != null)`; `isQuote = (quotedContentId non-empty || quotedContent != null)`; cả hai chỉ là metadata nội bộ của `AssembledUgcSubmit`, không gửi trực tiếp.

---

## 4. Native `libOKCore.so` — trạng thái phân tích

| Thuộc tính | Giá trị |
|---|---|
| `arm64-v8a/libOKCore.so` | 246.773.800 bytes, SHA-256 `c3e5015b3ed3c4b1a7505bce4f9a23a080bd73ea3bb937afe999e806f4eb5c50` |
| `armeabi-v7a/libOKCore.so` | 194.377.700 bytes, SHA-256 `da50d6120435d95361dd5880687ffd436d39c0641f574eca8598c1d77eef706a` |
| `NativeApi` methods | `encryptAccount/decryptAccount`, `encryptBody/decryptBody`, `encryptBodyOptimized/decryptBodyOptimized`, `encryptCommon/decryptCommon`, `encryptToken/decryptToken`, `encryptContent`, `getCryptoHandlerAesSecretKey`, `getDohRsaPrivacy`, `netApiSignature` |

- Không tìm thấy export trực tiếp `Java_com_okinc_okex_safety_NativeApi_*` trong `libOKCore.so` (kiểm tra `nm`/`readelf`); đăng ký JNI là **dynamic/obfuscated** (qua `JNI_OnLoad`/`RegisterNatives`), nên không thể map tên hàm native chỉ bằng symbol table.
- REA `inspect_android_jni_bindings` từ chối scan vì **capacity**: `"Selected native library exceeds the bounded library or cumulative scan-byte allowance."` Đây là giới hạn tài nguyên có chủ đích của REA, không phải lỗi artifact. Không có kết luận nào về binding native từ công cụ này.
- `objdump` không hỗ trợ AArch64 trong môi trường hiện tại; capstone chưa cài → không disassemble được. Các kết luận native dừng ở mức export/string/header và call-site tĩnh từ DEX.

**Đối với publish:** không cần native. Native chỉ xuất hiện ở:

- `OKEncryptInterceptor` (`C3952EMe`) — nhánh `Encrypt-Strategy: TIME` (publish không dùng);
- `OKSignInterceptor` — nhánh `OK-VERIFY-TOKEN` (publish không dùng);
- `NativeApi.getCryptoHandlerAesSecretKey`/`getDohRsaPrivacy` — cấu hình chung, không tham gia ký publish.

---

## 5. Tác động tới codebase Python và quy trình tối ưu

### 5.1. Những gì giữ nguyên (đã đúng)

- R1 payload, `SHA256withECDSA`, DER, Base64 NO_WRAP.
- `X-Client-Public-Key` = Base64 SPKI.
- `x-id = MD5(devId + "_" + encodedPath + "_" + UUID)`.
- Endpoint publish/presign/draft/permission/tag list.
- 4 field bắt buộc `publishId/content/channelId/group`.
- `publishId` milliseconds.

### 5.2. Những gì phải sửa/kiểm tra

1. **JSON body:** dùng `encodeDefaults=false` tương đương — bỏ `null`, list rỗng, chuỗi rỗng mặc định; **giữ** `group` kể cả `""`; không thêm `contentTags` rỗng. Nếu codebase đang gửi `"imageList": []`, `"contentTags": {"cashTagList": [], ...}`, hãy bỏ để khớp body app (server có thể vẫn nhận, nhưng fingerprint/body hash và tính nhất quán sẽ lệch).
2. **Thứ tự field JSON:** không bắt buộc cho chữ ký (server verify trên bytes nhận được), nhưng nên cố định để dễ so sánh capture.
3. **`HashTag.activityKey`:** luôn có; topic = `topicId`, link/mention = `""`.
4. **`HashTag.type`:** lấy từ `topicType` server; **không hardcode 4**. `deeplinkUrl` chỉ khi type 4.
5. **Mention:** phải có `deeplinkUrl = "planet/personal?authorId=<Uri.encode(authorId)>"` và `authorId`.
6. **Cash tag:** key là `coinName`.
7. **Ảnh:** `url/width/height/fileSize` thật; `displayUrl`/`srcKey` bỏ trống; không hardcode 1024.
8. **Risk headers:** không hardcode `tmx`/`headers-order-string-hash`. Nếu capture có, chúng đến từ risk callback của `DeviceInfoInterceptor`; hash là **MD5** trên tập header tại đúng stage. Nếu không tái tạo được SDK, dùng fallback `risk-params=fp-status=-2`, `fingerprint-id=devId`.
9. **`headers-order-string-hash`:** sửa công thức từ SHA-256 → **MD5**; sort lowercase; join không separator; hex lowercase.
10. **`Encrypt-Strategy`:** không thêm cho publish.
11. **Khoá R1:** đảm bảo private key Python tương ứng public key gửi đi và được server chấp nhận; app dùng Android Keystore alias `JwtTokenSecureKey` (không export). Nếu key lấy từ capture cũ, cần kiểm tra server còn bind không.
12. **Negative test:** gửi thiếu `activityKey`, sai `type`, thiếu `group` để xác nhận server trả lỗi — dùng trong test local/staging, không spam production.

### 5.3. Thiết kế profile versioned (cập nhật)

Thêm vào profile 6.194.0:

```yaml
json:
  encode_defaults: false
  media_type: application/json
signing:
  algo: SHA256withECDSA
  payload_post: "$encodedPath$body$timestampMs"
  headers:
    timestamp: X-Request-Timestamp
    signature: X-Client-Signature
    public_key: X-Client-Public-Key
  key_source: android_keystore_alias_JwtTokenSecureKey
  notes: private key non-exportable
risk:
  device_info_interceptor: true
  risk_callback: optional   # C5840Jei via DeviceInfoInterceptor
  hash:
    algo: md5
    input: sorted_lowercase_header_names_join_no_separator
    snapshot: chain_request_before_device_info_headers   # cho publish
  fallback:
    risk-params: fp-status=-2
    fingerprint-id: "$devId"
encrypt:
  enabled_for_publish: false
```

---

## 6. Những điểm vẫn chưa thể kết luận

- [Unknown] Tập header chính xác tại thời điểm `C5840Jei` tính hash trên **publish** trong runtime thực tế (phụ thuộc pre-custom interceptors và việc risk callback đã đăng ký hay chưa). Cần capture có timestamp tương ứng hoặc hook runtime.
- [Unknown] Cơ chế server bind `X-Client-Public-Key` với tài khoản/phiên (trust-on-first-use hay đăng ký riêng). Ảnh hưởng trực tiếp đến việc dùng key Python ngoài thiết bị.
- [Unknown] Đơn vị `scheduledAt` (ms hay sec) — chưa có body decompile của use case hẹn giờ.
- [Unknown] Presign URL có ký content-type hay không.
- [Unknown] `lua-version` runtime của 6.194.0 (server/config-driven).
- [Unknown] Hành vi enforcement của server với risk headers trên publish (có bắt buộc tmx/hash/X-FpToken không).
- [Unknown] Dynamic JNI registration trong `libOKCore.so` (REA bị chặn bởi capacity; không có bằng chứng binding).
- [Unknown] `activityCard`/`channelData` cho các loại bài đặc biệt (poll, article, prediction) — ngoài phạm vi bài text chuẩn.

---

## 7. Checklist xác minh nhanh cho lần cập nhật APK tiếp theo

1. `EKm` còn dùng kotlinx Json `encodeDefaults=false`? Kiểm tra `C3918EKr`/factory converter.
2. `PublishRequest` required mask còn `1289`? Field mới có default gì?
3. `HashTag`/`ContentTags`/`PublishImageItem` key names và default thay đổi?
4. `PlanetDependencyContainer.ActionBar` có override thêm interceptor/converter không?
5. `DeviceInfoInterceptor` có còn gọi risk callback (`C3922EKw.isConnected()`)?
6. `C5840Jei` có còn thêm tmx/hash? Công thức hash còn MD5?
7. `AsymmetricSignInterceptor` payload/gate/headers có đổi?
8. `OKEncryptInterceptor` gate còn `TIME` + POST?
9. `KsL` alias `JwtTokenSecureKey`/EC/SHA-256 còn giữ?
10. Endpoint `/priapi/v5/content/ugc/publish` và DTO response còn tương thích?

---

## 8. Kết luận

- Báo cáo gốc đúng về luồng tổng thể, endpoint, R1 payload, x-id, required fields và các lỗi P0/P1 của codebase.
- Ba đính chính quan trọng: **hash là MD5**, **Planet không cài `OKRiskSessionInterceptor`** (nhưng risk callback qua `DeviceInfoInterceptor` vẫn có thể thêm tmx/hash), và **JSON serialize bỏ default**.
- Đã xác định được nguồn khoá R1 (Android Keystore, alias `JwtTokenSecureKey`) — đây là ràng buộc lớn nhất nếu muốn tái tạo signing ngoài thiết bị.
- Các điểm còn lại chủ yếu là hành vi runtime/server và native dynamic registration; không cản trở việc hoàn thiện codebase publish nếu dùng đúng profile và key đã đăng ký.
