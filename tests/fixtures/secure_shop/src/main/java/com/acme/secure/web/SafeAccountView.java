package com.acme.secure.web;

import com.acme.secure.model.Account;
import com.fasterxml.jackson.annotation.JsonIgnore;

/** Same fields, the password is never serialised. */
public class SafeAccountView {

    private Long id;
    private String owner;
    @JsonIgnore
    private String password;

    public static SafeAccountView of(Account account) {
        SafeAccountView view = new SafeAccountView();
        view.id = account.getId();
        view.owner = account.getOwner();
        return view;
    }
}
