package com.acme.library.mapper;

import com.acme.library.domain.Book;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

@Mapper
public interface BookMapper {

    Book findById(@Param("id") String id);

    void insert(@Param("book") Book book);

    @Select("SELECT count(*) FROM books b JOIN loans l ON l.book_id = b.id")
    int countLoaned();
}
