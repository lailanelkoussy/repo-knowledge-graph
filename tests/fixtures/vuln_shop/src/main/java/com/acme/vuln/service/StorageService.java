package com.acme.vuln.service;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.file.Files;
import java.nio.file.Paths;
import org.springframework.stereotype.Service;

@Service
public class StorageService {

    public byte[] read(String name) {
        try {
            return Files.readAllBytes(Paths.get("/data/uploads/" + name));
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
    }

    public byte[] readDefault() {
        return read("default.txt");
    }
}
