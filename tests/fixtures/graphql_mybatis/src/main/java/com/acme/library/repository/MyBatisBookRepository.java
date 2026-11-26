package com.acme.library.repository;

import com.acme.library.domain.Book;
import com.acme.library.mapper.BookMapper;
import org.springframework.stereotype.Repository;

@Repository
public class MyBatisBookRepository implements BookRepository {

    private final BookMapper bookMapper;

    public MyBatisBookRepository(BookMapper bookMapper) {
        this.bookMapper = bookMapper;
    }

    @Override
    public Book find(String id) {
        return bookMapper.findById(id);
    }

    @Override
    public void save(Book book) {
        bookMapper.insert(book);
    }
}
