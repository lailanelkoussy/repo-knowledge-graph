package com.acme.secure.web;

import com.acme.secure.model.Account;
import jakarta.validation.constraints.NotBlank;

public class RegisterRequest {

    @NotBlank
    private String username;
    @NotBlank
    private String password;
    private String role;

    public Account toAccount() {
        return new Account(username, password, role);
    }
}
