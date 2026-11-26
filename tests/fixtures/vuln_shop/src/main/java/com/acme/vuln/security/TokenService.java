package com.acme.vuln.security;

import java.security.SecureRandom;
import java.util.Collections;
import java.util.List;
import java.util.Random;

public class TokenService {

    public String generateResetToken() {
        return Integer.toHexString(new Random().nextInt());
    }

    public String generateSessionToken() {
        return Long.toHexString(new SecureRandom().nextLong());
    }

    public void shuffle(List<String> items) {
        Collections.shuffle(items, new Random(42));
    }
}
