package com.acme.vuln.util;

import java.security.MessageDigest;
import javax.crypto.Cipher;
import javax.crypto.spec.SecretKeySpec;

public final class CryptoUtil {

    private static final String ENCRYPTION_KEY = "0123456789abcdef";

    private CryptoUtil() {
    }

    public static byte[] md5(byte[] data) throws Exception {
        return MessageDigest.getInstance("MD5").digest(data);
    }

    public static byte[] sha256(byte[] data) throws Exception {
        return MessageDigest.getInstance("SHA-256").digest(data);
    }

    public static Cipher des() throws Exception {
        return Cipher.getInstance("DES");
    }

    public static Cipher gcm() throws Exception {
        return Cipher.getInstance("AES/GCM/NoPadding");
    }

    public static SecretKeySpec key() {
        return new SecretKeySpec(ENCRYPTION_KEY.getBytes(), "AES");
    }

    public static SecretKeySpec key(byte[] material) {
        return new SecretKeySpec(material, "AES");
    }
}
