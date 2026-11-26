package com.acme.secure.web;

import com.acme.secure.model.Account;

/** Response type leaking the password hash. */
public class AccountView {

    private Long id;
    private String owner;
    private String password;

    public static AccountView of(Account account) {
        AccountView view = new AccountView();
        view.id = account.getId();
        view.owner = account.getOwner();
        view.password = account.getPassword();
        return view;
    }
}
