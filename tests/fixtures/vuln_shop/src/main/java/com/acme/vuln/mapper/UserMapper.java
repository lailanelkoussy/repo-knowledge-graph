package com.acme.vuln.mapper;

import java.util.List;
import java.util.Map;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;

@Mapper
public interface UserMapper {

    List<Map<String, Object>> listOrdered(@Param("order") String order);

    Map<String, Object> byId(@Param("id") long id);
}
