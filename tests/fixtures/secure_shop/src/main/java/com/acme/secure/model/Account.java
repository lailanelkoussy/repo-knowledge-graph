package com.acme.secure.model;

import jakarta.persistence.Entity;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.Id;

@Entity
public class Account {

    @Id
    @GeneratedValue
    private Long id;
    private String owner;
    private String password;
    private String role;

    protected Account() {
    }

    public Account(String owner, String password, String role) {
        this.owner = owner;
        this.password = password;
        this.role = role;
    }

    public Long getId() {
        return id;
    }

    public String getOwner() {
        return owner;
    }

    public String getPassword() {
        return password;
    }
}
