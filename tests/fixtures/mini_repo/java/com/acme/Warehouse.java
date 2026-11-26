package com.acme;

public class Warehouse extends Inventory {
    public int capacity() {
        return countItems() * 2;
    }
}
