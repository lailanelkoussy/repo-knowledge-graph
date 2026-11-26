package com.acme.library.repository;

import com.acme.library.domain.Book;
import org.springframework.stereotype.Repository;

@Repository
public interface BookRepository {

    Book find(String id);

    void save(Book book);
}
