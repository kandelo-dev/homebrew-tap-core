/*
 * Copyright (c) 2026 Automattic, Inc.
 *
 * This program is free software; you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation; either version 2 of the License, or
 * (at your option) any later version.
 */

#define _XOPEN_SOURCE 700

#include <errno.h>
#include <getopt.h>
#include <langinfo.h>
#include <locale.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <wchar.h>

#include <mcpp_lib.h>
#include <tree_sitter/api.h>

extern const TSLanguage *tree_sitter_c(void);

typedef enum {
  SYMBOL_LINKAGE_NONE,
  SYMBOL_LINKAGE_INTERNAL,
  SYMBOL_LINKAGE_EXTERNAL,
} SymbolLinkage;

typedef struct {
  uint32_t start_byte;
  uint32_t end_byte;
  unsigned depth;
} LexicalScope;

typedef struct {
  char *name;
  char *path;
  char *function;
  unsigned line;
  uint32_t position;
  LexicalScope scope;
  size_t translation_unit;
  size_t symbol_identity;
  bool declaration;
  bool ordinary_identifier;
  bool declares_function;
  bool function_symbol;
  bool macro_argument;
  bool macro_replacement;
  SymbolLinkage linkage;
} Reference;

typedef struct {
  Reference *items;
  size_t length;
  size_t capacity;
} ReferenceList;

typedef enum {
  PREPROCESSOR_DEFINE,
  PREPROCESSOR_INCLUDE,
  PREPROCESSOR_UNDEFINE,
} PreprocessorOptionKind;

typedef struct {
  PreprocessorOptionKind kind;
  char *name;
  char *argument;
} PreprocessorOption;

typedef struct {
  PreprocessorOption *items;
  size_t length;
  size_t capacity;
} PreprocessorOptions;

typedef struct {
  char **paths;
  unsigned *lines;
  size_t count;
} SourceMap;

typedef struct {
  char *path;
  char *text;
  size_t length;
  size_t *line_offsets;
  size_t line_count;
} SourceFile;

typedef struct {
  SourceFile *items;
  size_t length;
  size_t capacity;
} SourceFiles;

typedef struct {
  char *name;
  unsigned start_line;
  unsigned start_column;
  unsigned end_line;
  unsigned end_column;
} MarkerRange;

typedef enum {
  EXPANSION_ORIGIN_NONE,
  EXPANSION_ORIGIN_ARGUMENT,
  EXPANSION_ORIGIN_DEFINITION,
} ExpansionOrigin;

typedef struct {
  MarkerRange range;
  char *path;
  ExpansionOrigin origin;
} MacroSourceRange;

typedef struct {
  MacroSourceRange *items;
  size_t length;
  size_t capacity;
} MacroSourceRanges;

typedef struct {
  char *path;
  unsigned start_line;
  unsigned end_line;
  ExpansionOrigin origin;
} ExpansionFrame;

typedef struct {
  ExpansionFrame *items;
  size_t length;
  size_t capacity;
} ExpansionFrames;

typedef struct {
  const char *source;
  const SourceMap *map;
  ReferenceList *references;
  SourceFiles *source_files;
  size_t translation_unit;
  MacroSourceRanges macro_arguments;
  MacroSourceRanges macro_definitions;
  ExpansionFrames expansions;
  bool condition_markers;
  int status;
} WalkContext;

static void *checked_realloc(void *pointer, size_t count, size_t size) {
  void *result;

  if (size != 0 && count > SIZE_MAX / size) {
    fprintf(stderr, "cxref: allocation size overflow\n");
    exit(2);
  }
  result = realloc(pointer, count * size);
  if (result == NULL) {
    fprintf(stderr, "cxref: out of memory\n");
    exit(2);
  }
  return result;
}

static char *copy_text(const char *text, size_t length) {
  char *copy = checked_realloc(NULL, length + 1, 1);
  memcpy(copy, text, length);
  copy[length] = '\0';
  return copy;
}

static char *copy_string(const char *text) {
  return copy_text(text, strlen(text));
}

static char *join_option(const char *option, const char *argument) {
  size_t option_length = strlen(option);
  size_t argument_length = strlen(argument);
  char *result = checked_realloc(NULL, option_length + argument_length + 1, 1);

  memcpy(result, option, option_length);
  memcpy(result + option_length, argument, argument_length + 1);
  return result;
}

static char *preprocessor_encoding_option(void) {
  const char *codeset = nl_langinfo(CODESET);
  char normalized[64];
  size_t input = 0;
  size_t output = 0;

  if (codeset == NULL || codeset[0] == '\0') {
    return copy_string("-ec");
  }
  if (strlen(codeset) >= 20) {
    fprintf(stderr, "cxref: locale encoding name is too long: %s\n", codeset);
    exit(2);
  }
  while (codeset[input] != '\0' && output + 1 < sizeof(normalized)) {
    char character = codeset[input++];
    if (character == '-' || character == '_' || character == '.') {
      continue;
    }
    if (character >= 'A' && character <= 'Z') {
      character = (char)(character - 'A' + 'a');
    }
    normalized[output++] = character;
  }
  normalized[output] = '\0';

  if (strcmp(normalized, "utf8") == 0) {
    return copy_string("-eutf8");
  }
  if (strcmp(normalized, "ascii") == 0 || strcmp(normalized, "usascii") == 0 ||
      strcmp(normalized, "ansix341968") == 0) {
    return copy_string("-ec");
  }
  return join_option("-e", codeset);
}

static size_t macro_name_length(const char *argument) {
  const char *equals = strchr(argument, '=');
  return equals == NULL ? strlen(argument) : (size_t)(equals - argument);
}

static void preprocessor_options_add(PreprocessorOptions *options,
                                     PreprocessorOptionKind kind,
                                     const char *argument) {
  size_t name_length =
      kind == PREPROCESSOR_INCLUDE ? 0 : macro_name_length(argument);

  if (kind != PREPROCESSOR_INCLUDE && name_length == 0) {
    fprintf(stderr, "cxref: empty macro name\n");
    exit(2);
  }

  if (options->length == options->capacity) {
    options->capacity = options->capacity == 0 ? 8 : options->capacity * 2;
    options->items = checked_realloc(options->items, options->capacity,
                                     sizeof(*options->items));
  }
  options->items[options->length++] = (PreprocessorOption){
      .kind = kind,
      .name = kind == PREPROCESSOR_INCLUDE ? NULL
                                           : copy_text(argument, name_length),
      .argument = copy_string(argument),
  };
}

static bool
preprocessor_option_is_superseded(const PreprocessorOptions *options,
                                  size_t option_index) {
  const PreprocessorOption *option = &options->items[option_index];
  size_t index;

  if (option->kind == PREPROCESSOR_INCLUDE) {
    return false;
  }
  for (index = option_index + 1; index < options->length; ++index) {
    const PreprocessorOption *later = &options->items[index];
    if (later->kind != PREPROCESSOR_INCLUDE &&
        strcmp(option->name, later->name) == 0) {
      return true;
    }
  }
  return false;
}

static void preprocessor_options_delete(PreprocessorOptions *options) {
  size_t index;
  for (index = 0; index < options->length; ++index) {
    free(options->items[index].name);
    free(options->items[index].argument);
  }
  free(options->items);
}

static Reference *reference_list_add(ReferenceList *list, const char *name,
                                     const char *path, const char *function,
                                     unsigned line, bool declaration,
                                     bool function_symbol,
                                     size_t translation_unit) {
  if (name[0] == '\0' || path == NULL || path[0] == '\0' || line == 0) {
    return NULL;
  }
  if (list->length == list->capacity) {
    list->capacity = list->capacity == 0 ? 64 : list->capacity * 2;
    list->items =
        checked_realloc(list->items, list->capacity, sizeof(*list->items));
  }
  list->items[list->length++] = (Reference){
      .name = copy_string(name),
      .path = copy_string(path),
      .function = function == NULL ? NULL : copy_string(function),
      .line = line,
      .translation_unit = translation_unit,
      .declaration = declaration,
      .function_symbol = function_symbol,
  };
  return &list->items[list->length - 1];
}

static void reference_list_append(ReferenceList *destination,
                                  ReferenceList *source) {
  size_t required = destination->length + source->length;
  if (required > destination->capacity) {
    destination->capacity = required;
    destination->items = checked_realloc(
        destination->items, destination->capacity, sizeof(*destination->items));
  }
  memcpy(destination->items + destination->length, source->items,
         source->length * sizeof(*source->items));
  destination->length = required;
  free(source->items);
  *source = (ReferenceList){0};
}

static void reference_list_delete(ReferenceList *list) {
  size_t index;
  for (index = 0; index < list->length; ++index) {
    free(list->items[index].name);
    free(list->items[index].path);
    free(list->items[index].function);
  }
  free(list->items);
}

static char *absolute_operand(const char *path) {
  char *directory;
  char *absolute;
  size_t capacity = 256;

  if (path[0] == '/') {
    return copy_string(path);
  }
  while (true) {
    directory = checked_realloc(NULL, capacity, 1);
    if (getcwd(directory, capacity) != NULL) {
      break;
    }
    free(directory);
    if (errno != ERANGE || capacity > SIZE_MAX / 2) {
      fprintf(stderr, "cxref: cannot determine current directory: %s\n",
              strerror(errno));
      return NULL;
    }
    capacity *= 2;
  }
  absolute = checked_realloc(NULL, strlen(directory) + strlen(path) + 2, 1);
  snprintf(absolute, strlen(directory) + strlen(path) + 2, "%s/%s", directory,
           path);
  free(directory);
  return absolute;
}

static SourceFile *source_files_get(SourceFiles *files, const char *path) {
  SourceFile *file;
  FILE *stream;
  long length;
  size_t index;

  for (index = 0; index < files->length; ++index) {
    if (strcmp(files->items[index].path, path) == 0) {
      return &files->items[index];
    }
  }
  stream = fopen(path, "rb");
  if (stream == NULL) {
    fprintf(stderr, "cxref: %s: %s\n", path, strerror(errno));
    return NULL;
  }
  if (fseek(stream, 0, SEEK_END) != 0 || (length = ftell(stream)) < 0 ||
      fseek(stream, 0, SEEK_SET) != 0) {
    fprintf(stderr, "cxref: %s: cannot determine input size\n", path);
    fclose(stream);
    return NULL;
  }
  if (files->length == files->capacity) {
    files->capacity = files->capacity == 0 ? 8 : files->capacity * 2;
    files->items =
        checked_realloc(files->items, files->capacity, sizeof(*files->items));
  }
  file = &files->items[files->length++];
  *file = (SourceFile){
      .path = copy_string(path),
      .length = (size_t)length,
  };
  file->text = checked_realloc(NULL, file->length + 1, 1);
  if (fread(file->text, 1, file->length, stream) != file->length) {
    fprintf(stderr, "cxref: %s: cannot read input\n", path);
    fclose(stream);
    free(file->path);
    free(file->text);
    --files->length;
    return NULL;
  }
  fclose(stream);
  file->text[file->length] = '\0';

  file->line_count = 1;
  for (index = 0; index < file->length; ++index) {
    if (file->text[index] == '\n') {
      ++file->line_count;
    }
  }
  file->line_offsets =
      checked_realloc(NULL, file->line_count, sizeof(*file->line_offsets));
  file->line_offsets[0] = 0;
  file->line_count = 1;
  for (index = 0; index < file->length; ++index) {
    if (file->text[index] == '\n') {
      file->line_offsets[file->line_count++] = index + 1;
    }
  }
  return file;
}

static void source_files_delete(SourceFiles *files) {
  size_t index;
  for (index = 0; index < files->length; ++index) {
    free(files->items[index].path);
    free(files->items[index].text);
    free(files->items[index].line_offsets);
  }
  free(files->items);
}

static bool source_coordinate_offset(const SourceFile *file, unsigned line,
                                     unsigned column, size_t *offset) {
  size_t line_start;
  size_t line_limit;

  if (line == 0 || column == 0 || line > file->line_count) {
    return false;
  }
  line_start = file->line_offsets[line - 1];
  line_limit =
      line < file->line_count ? file->line_offsets[line] : file->length;
  if ((size_t)(column - 1) > line_limit - line_start) {
    return false;
  }
  *offset = line_start + column - 1;
  return true;
}

static bool parse_line_directive(const char *line, size_t length,
                                 unsigned *number, char **path) {
  const char *cursor = line;
  const char *end = line + length;
  char *number_end;
  unsigned long parsed;
  const char *path_start;

  while (cursor < end && (*cursor == ' ' || *cursor == '\t')) {
    ++cursor;
  }
  if (cursor == end || *cursor++ != '#') {
    return false;
  }
  while (cursor < end && (*cursor == ' ' || *cursor == '\t')) {
    ++cursor;
  }
  if ((size_t)(end - cursor) >= 4 && memcmp(cursor, "line", 4) == 0 &&
      (cursor + 4 == end || cursor[4] == ' ' || cursor[4] == '\t')) {
    cursor += 4;
    while (cursor < end && (*cursor == ' ' || *cursor == '\t')) {
      ++cursor;
    }
  }
  if (cursor == end || *cursor < '0' || *cursor > '9') {
    return false;
  }

  errno = 0;
  parsed = strtoul(cursor, &number_end, 10);
  if (errno != 0 || parsed == 0 || parsed > UINT32_MAX || number_end > end) {
    return false;
  }
  cursor = number_end;
  while (cursor < end && (*cursor == ' ' || *cursor == '\t')) {
    ++cursor;
  }
  if (cursor == end || *cursor++ != '"') {
    return false;
  }
  path_start = cursor;
  while (cursor < end && *cursor != '"') {
    ++cursor;
  }
  if (cursor == end) {
    return false;
  }

  *number = (unsigned)parsed;
  *path = copy_text(path_start, (size_t)(cursor - path_start));
  return true;
}

static SourceMap source_map_build(const char *source, const char *input_path) {
  SourceMap map = {0};
  const char *cursor;
  const char *line_end;
  char *current_path = copy_string(input_path);
  unsigned current_line = 1;
  size_t index = 0;

  map.count = 1;
  for (cursor = source; *cursor != '\0'; ++cursor) {
    if (*cursor == '\n') {
      ++map.count;
    }
  }
  map.paths = checked_realloc(NULL, map.count, sizeof(*map.paths));
  map.lines = checked_realloc(NULL, map.count, sizeof(*map.lines));
  memset(map.paths, 0, map.count * sizeof(*map.paths));
  memset(map.lines, 0, map.count * sizeof(*map.lines));

  cursor = source;
  while (index < map.count) {
    unsigned directive_line;
    char *directive_path = NULL;
    line_end = strchr(cursor, '\n');
    if (line_end == NULL) {
      line_end = cursor + strlen(cursor);
    }
    if (parse_line_directive(cursor, (size_t)(line_end - cursor),
                             &directive_line, &directive_path)) {
      free(current_path);
      current_path = directive_path;
      current_line = directive_line;
    } else {
      map.paths[index] = copy_string(current_path);
      map.lines[index] = current_line++;
    }
    ++index;
    cursor = *line_end == '\n' ? line_end + 1 : line_end;
  }
  free(current_path);
  return map;
}

static void source_map_delete(SourceMap *map) {
  size_t index;
  for (index = 0; index < map->count; ++index) {
    free(map->paths[index]);
  }
  free(map->paths);
  free(map->lines);
}

static char *parser_source_build(const char *source) {
  char *parser_source = copy_string(source);
  const char *cursor = source;
  char *output = parser_source;

  while (*cursor != '\0') {
    const char *line_end = strchr(cursor, '\n');
    size_t length;
    unsigned directive_line;
    char *directive_path = NULL;
    size_t index;

    if (line_end == NULL) {
      line_end = cursor + strlen(cursor);
    }
    length = (size_t)(line_end - cursor);
    if (parse_line_directive(cursor, length, &directive_line,
                             &directive_path)) {
      for (index = 0; index < length; ++index) {
        if (output[index] != '\r') {
          output[index] = ' ';
        }
      }
      free(directive_path);
    }
    output += length;
    cursor = line_end;
    if (*cursor == '\n') {
      ++cursor;
      ++output;
    }
  }
  return parser_source;
}

static char *node_text(TSNode node, const char *source) {
  return copy_text(source + ts_node_start_byte(node),
                   ts_node_end_byte(node) - ts_node_start_byte(node));
}

static TSNode first_identifier(TSNode node) {
  uint32_t index;
  if (strcmp(ts_node_type(node), "identifier") == 0) {
    return node;
  }
  for (index = 0; index < ts_node_named_child_count(node); ++index) {
    TSNode result = first_identifier(ts_node_named_child(node, index));
    if (!ts_node_is_null(result)) {
      return result;
    }
  }
  return (TSNode){0};
}

static const char *field_for_child(TSNode parent, TSNode child) {
  uint32_t index;
  for (index = 0; index < ts_node_child_count(parent); ++index) {
    if (ts_node_eq(ts_node_child(parent, index), child)) {
      return ts_node_field_name_for_child(parent, index);
    }
  }
  return NULL;
}

static bool is_declarator_wrapper(const char *type) {
  return strcmp(type, "array_declarator") == 0 ||
         strcmp(type, "attributed_declarator") == 0 ||
         strcmp(type, "function_declarator") == 0 ||
         strcmp(type, "init_declarator") == 0 ||
         strcmp(type, "parenthesized_declarator") == 0 ||
         strcmp(type, "pointer_declarator") == 0;
}

static bool identifier_is_declaration(TSNode node) {
  TSNode child = node;

  while (true) {
    TSNode parent = ts_node_parent(child);
    const char *parent_type;
    const char *field;
    if (ts_node_is_null(parent)) {
      return false;
    }
    parent_type = ts_node_type(parent);
    field = field_for_child(parent, child);

    if (is_declarator_wrapper(parent_type)) {
      if (field != NULL && strcmp(field, "declarator") == 0) {
        child = parent;
        continue;
      }
      if (strcmp(parent_type, "parenthesized_declarator") == 0 &&
          field == NULL) {
        child = parent;
        continue;
      }
      return false;
    }
    if (strcmp(parent_type, "declaration") == 0 ||
        strcmp(parent_type, "function_definition") == 0 ||
        strcmp(parent_type, "parameter_declaration") == 0) {
      return field != NULL && strcmp(field, "declarator") == 0;
    }
    if (strcmp(parent_type, "parameter_list") == 0) {
      TSNode declarator = ts_node_parent(parent);
      if (!ts_node_is_null(declarator) &&
          strcmp(ts_node_type(declarator), "function_declarator") == 0) {
        return true;
      }
    }
    if (strcmp(parent_type, "enumerator") == 0) {
      return field == NULL || strcmp(field, "name") == 0;
    }
    if (strcmp(parent_type, "expression_statement") == 0 ||
        strcmp(parent_type, "call_expression") == 0 ||
        strcmp(parent_type, "return_statement") == 0) {
      return false;
    }
    child = parent;
  }
}

static bool identifier_is_function(TSNode node) {
  TSNode child = node;
  bool pointer_declarator_seen = false;

  while (true) {
    TSNode parent = ts_node_parent(child);
    const char *type;
    const char *field;
    if (ts_node_is_null(parent)) {
      return false;
    }
    type = ts_node_type(parent);
    field = field_for_child(parent, child);
    if (strcmp(type, "function_declarator") == 0 && field != NULL &&
        strcmp(field, "declarator") == 0) {
      return !pointer_declarator_seen;
    }
    if (strcmp(type, "pointer_declarator") == 0 && field != NULL &&
        strcmp(field, "declarator") == 0) {
      pointer_declarator_seen = true;
    }
    if (strcmp(type, "parameter_declaration") == 0 ||
        strcmp(type, "expression_statement") == 0 ||
        strcmp(type, "return_statement") == 0) {
      return false;
    }
    child = parent;
  }
}

static TSNode identifier_declaration_owner(TSNode node) {
  TSNode current = node;

  while (!ts_node_is_null(current)) {
    const char *type = ts_node_type(current);
    if (strcmp(type, "declaration") == 0 ||
        strcmp(type, "function_definition") == 0 ||
        strcmp(type, "parameter_declaration") == 0) {
      return current;
    }
    current = ts_node_parent(current);
  }
  return (TSNode){0};
}

static bool node_has_storage_class(TSNode node, const char *source,
                                   const char *storage_class) {
  uint32_t index;

  for (index = 0; index < ts_node_named_child_count(node); ++index) {
    TSNode child = ts_node_named_child(node, index);
    if (strcmp(ts_node_type(child), "storage_class_specifier") == 0) {
      char *text = node_text(child, source);
      bool matches = strcmp(text, storage_class) == 0;
      free(text);
      if (matches) {
        return true;
      }
    }
  }
  return false;
}

static LexicalScope lexical_scope_for_node(TSNode node, unsigned depth) {
  return (LexicalScope){
      .start_byte = ts_node_start_byte(node),
      .end_byte = ts_node_end_byte(node),
      .depth = depth,
  };
}

static const ExpansionFrame *active_expansion(const WalkContext *context,
                                              ExpansionOrigin origin) {
  size_t index;

  for (index = context->expansions.length; index > 0; --index) {
    const ExpansionFrame *frame = &context->expansions.items[index - 1];
    if (frame->origin == origin) {
      return frame;
    }
  }
  return NULL;
}

static const ExpansionFrame *
enclosing_macro_expansion(const WalkContext *context) {
  size_t index;

  if (context->expansions.length < 2) {
    return NULL;
  }
  for (index = context->expansions.length - 1; index > 0; --index) {
    const ExpansionFrame *frame = &context->expansions.items[index - 1];
    if (frame->origin != EXPANSION_ORIGIN_NONE) {
      return frame;
    }
  }
  return NULL;
}

static bool nullable_string_equal(const char *left, const char *right) {
  return (left == NULL && right == NULL) ||
         (left != NULL && right != NULL && strcmp(left, right) == 0);
}

static Reference *macro_argument_reference(WalkContext *context,
                                           const ExpansionFrame *frame,
                                           const char *name,
                                           const char *function) {
  size_t index;

  /* Upgrade the source argument entry instead of emitting a second reference
   * for the expanded AST node. Discarded and stringized arguments stay as the
   * source-only entries recorded by scan_source_range(). */
  for (index = 0; index < context->references->length; ++index) {
    Reference *candidate = &context->references->items[index];
    if (candidate->macro_argument && !candidate->ordinary_identifier &&
        strcmp(candidate->name, name) == 0 &&
        strcmp(candidate->path, frame->path) == 0 &&
        nullable_string_equal(candidate->function, function) &&
        candidate->line >= frame->start_line &&
        candidate->line <= frame->end_line) {
      return candidate;
    }
  }
  return NULL;
}

static Reference *macro_definition_reference(WalkContext *context,
                                             const ExpansionFrame *frame,
                                             const char *name,
                                             Reference **template) {
  size_t index;

  /* Replacement tokens report their definition location, but their binding
   * and declaration role come from each expanded AST occurrence. */
  *template = NULL;
  for (index = 0; index < context->references->length; ++index) {
    Reference *candidate = &context->references->items[index];
    if (!candidate->macro_replacement || strcmp(candidate->name, name) != 0 ||
        strcmp(candidate->path, frame->path) != 0 ||
        candidate->line < frame->start_line ||
        candidate->line > frame->end_line) {
      continue;
    }
    if (*template == NULL) {
      *template = candidate;
    }
    if (!candidate->ordinary_identifier) {
      return candidate;
    }
  }
  return NULL;
}

static void add_identifier(WalkContext *context, TSNode node,
                           const char *function, LexicalScope scope) {
  TSPoint point = ts_node_start_point(node);
  const ExpansionFrame *argument_expansion =
      active_expansion(context, EXPANSION_ORIGIN_ARGUMENT);
  const ExpansionFrame *definition_expansion =
      argument_expansion == NULL
          ? active_expansion(context, EXPANSION_ORIGIN_DEFINITION)
          : NULL;
  const ExpansionFrame *expansion =
      argument_expansion != NULL ? argument_expansion : definition_expansion;
  TSNode owner;
  Reference *reference;
  Reference *definition_template = NULL;
  const char *path;
  const char *output_function;
  unsigned line;
  char *name;
  bool declaration;
  bool function_symbol;
  SymbolLinkage linkage = SYMBOL_LINKAGE_NONE;

  if (context->expansions.length > 0 && expansion == NULL) {
    return;
  }
  if (expansion == NULL && (point.row >= context->map->count ||
                            context->map->paths[point.row] == NULL)) {
    return;
  }
  path = expansion == NULL ? context->map->paths[point.row] : expansion->path;
  line = expansion == NULL ? context->map->lines[point.row]
                           : expansion->start_line;
  name = node_text(node, context->source);
  declaration = identifier_is_declaration(node);
  function_symbol = identifier_is_function(node);
  owner = identifier_declaration_owner(node);
  if (function_symbol && !ts_node_is_null(owner) &&
      node_has_storage_class(owner, context->source, "typedef")) {
    function_symbol = false;
  }
  if (function_symbol) {
    linkage = !ts_node_is_null(owner) &&
                      node_has_storage_class(owner, context->source, "static")
                  ? SYMBOL_LINKAGE_INTERNAL
                  : SYMBOL_LINKAGE_EXTERNAL;
  }
  output_function = function_symbol ? NULL : function;
  if (argument_expansion != NULL) {
    reference =
        macro_argument_reference(context, argument_expansion, name, function);
  } else if (definition_expansion != NULL) {
    reference = macro_definition_reference(context, definition_expansion, name,
                                           &definition_template);
    if (!function_symbol && definition_template != NULL) {
      output_function = definition_template->function;
    }
  } else {
    reference = NULL;
  }
  if (reference == NULL) {
    reference = reference_list_add(context->references, name, path,
                                   output_function, line, declaration,
                                   function_symbol, context->translation_unit);
  } else {
    reference->declaration = declaration;
    reference->function_symbol = function_symbol;
    if (function_symbol) {
      free(reference->function);
      reference->function = NULL;
    }
  }
  if (reference != NULL) {
    reference->position = ts_node_start_byte(node);
    reference->scope = scope;
    reference->ordinary_identifier = true;
    reference->declares_function = function_symbol;
    reference->linkage = linkage;
  }
  free(name);
}

static bool identifier_start(unsigned char character);
static bool identifier_continue(unsigned char character);

static bool parse_marker_number(const char **cursor, unsigned *number) {
  char *number_end;
  unsigned long parsed;

  errno = 0;
  parsed = strtoul(*cursor, &number_end, 10);
  if (errno != 0 || number_end == *cursor || parsed == 0 ||
      parsed > UINT32_MAX) {
    return false;
  }
  *cursor = number_end;
  *number = (unsigned)parsed;
  return true;
}

static bool parse_marker_range(const char *text, char marker,
                               MarkerRange *range) {
  const char *cursor = text + 3;
  const char *name_start;

  if (strncmp(text, "/*", 2) != 0 || text[2] != marker) {
    return false;
  }
  name_start = cursor;
  while ((*cursor >= 'A' && *cursor <= 'Z') ||
         (*cursor >= 'a' && *cursor <= 'z') ||
         (*cursor >= '0' && *cursor <= '9') || *cursor == '_') {
    ++cursor;
  }
  if (cursor == name_start || (*cursor != ' ' && *cursor != '\t')) {
    return false;
  }
  while (*cursor == ' ' || *cursor == '\t') {
    ++cursor;
  }
  if (!parse_marker_number(&cursor, &range->start_line) || *cursor++ != ':' ||
      !parse_marker_number(&cursor, &range->start_column) || *cursor++ != '-' ||
      !parse_marker_number(&cursor, &range->end_line) || *cursor++ != ':' ||
      !parse_marker_number(&cursor, &range->end_column) ||
      strcmp(cursor, "*/") != 0) {
    return false;
  }
  range->name = copy_text(name_start, strcspn(name_start, " \t"));
  return true;
}

static bool parse_argument_marker(const char *text, MarkerRange *range) {
  const char *cursor = text + 3;
  const char *name_start;

  if (strncmp(text, "/*!", 3) != 0) {
    return false;
  }
  name_start = cursor;
  while (identifier_continue((unsigned char)*cursor) || *cursor == ':' ||
         *cursor == '-') {
    ++cursor;
  }
  if (cursor == name_start) {
    return false;
  }
  range->name = copy_text(name_start, (size_t)(cursor - name_start));
  if (strcmp(cursor, "*/") == 0) {
    return true;
  }
  if (*cursor != ' ' && *cursor != '\t') {
    free(range->name);
    range->name = NULL;
    return false;
  }
  while (*cursor == ' ' || *cursor == '\t') {
    ++cursor;
  }
  if (!parse_marker_number(&cursor, &range->start_line) || *cursor++ != ':' ||
      !parse_marker_number(&cursor, &range->start_column) || *cursor++ != '-' ||
      !parse_marker_number(&cursor, &range->end_line) || *cursor++ != ':' ||
      !parse_marker_number(&cursor, &range->end_column) ||
      strcmp(cursor, "*/") != 0) {
    free(range->name);
    *range = (MarkerRange){0};
    return false;
  }
  return true;
}

static bool parse_substitution_marker(const char *text, char **name) {
  const char *cursor = text + 3;
  const char *name_start;
  bool parameter_key = false;

  if (strncmp(text, "/*<", 3) != 0) {
    return false;
  }
  name_start = cursor;
  while (identifier_continue((unsigned char)*cursor) || *cursor == ':' ||
         *cursor == '-') {
    if (*cursor == ':') {
      parameter_key = true;
    }
    ++cursor;
  }
  if (!parameter_key || cursor == name_start || strcmp(cursor, "*/") != 0) {
    return false;
  }
  *name = copy_text(name_start, (size_t)(cursor - name_start));
  return true;
}

static bool parse_macro_expansion_marker(const char *text, char **name) {
  const char *cursor = text + 3;
  const char *name_start;

  if (strncmp(text, "/*<", 3) != 0) {
    return false;
  }
  name_start = cursor;
  while (identifier_continue((unsigned char)*cursor)) {
    ++cursor;
  }
  if (cursor == name_start || strcmp(cursor, "*/") != 0) {
    return false;
  }
  *name = copy_text(name_start, (size_t)(cursor - name_start));
  return true;
}

static MacroSourceRange *macro_source_range_find(MacroSourceRanges *ranges,
                                                 const char *name) {
  size_t index;

  for (index = 0; index < ranges->length; ++index) {
    if (strcmp(ranges->items[index].range.name, name) == 0) {
      return &ranges->items[index];
    }
  }
  return NULL;
}

static void macro_source_range_record(MacroSourceRanges *ranges,
                                      const char *path, MarkerRange *range,
                                      ExpansionOrigin origin) {
  MacroSourceRange *source_range = macro_source_range_find(ranges, range->name);

  if (source_range == NULL) {
    if (ranges->length == ranges->capacity) {
      ranges->capacity = ranges->capacity == 0 ? 8 : ranges->capacity * 2;
      ranges->items = checked_realloc(ranges->items, ranges->capacity,
                                      sizeof(*ranges->items));
    }
    source_range = &ranges->items[ranges->length++];
    *source_range = (MacroSourceRange){0};
  } else {
    free(source_range->range.name);
    free(source_range->path);
  }
  source_range->range = *range;
  source_range->path = copy_string(path);
  source_range->origin = origin;
  range->name = NULL;
}

static void expansion_frame_push(WalkContext *context,
                                 const MacroSourceRange *source_range) {
  ExpansionFrame *frame;

  if (context->expansions.length == context->expansions.capacity) {
    context->expansions.capacity = context->expansions.capacity == 0
                                       ? 8
                                       : context->expansions.capacity * 2;
    context->expansions.items =
        checked_realloc(context->expansions.items, context->expansions.capacity,
                        sizeof(*context->expansions.items));
  }
  frame = &context->expansions.items[context->expansions.length++];
  *frame = (ExpansionFrame){0};
  if (source_range != NULL && source_range->range.start_line != 0) {
    frame->path = copy_string(source_range->path);
    frame->start_line = source_range->range.start_line;
    frame->end_line = source_range->range.end_line;
    frame->origin = source_range->origin;
  }
}

static void expansion_frame_pop(WalkContext *context) {
  if (context->expansions.length == 0) {
    return;
  }
  free(context->expansions.items[context->expansions.length - 1].path);
  --context->expansions.length;
}

static void macro_source_ranges_delete(MacroSourceRanges *ranges) {
  size_t index;

  for (index = 0; index < ranges->length; ++index) {
    free(ranges->items[index].range.name);
    free(ranges->items[index].path);
  }
  free(ranges->items);
}

static void macro_expansion_state_delete(WalkContext *context) {
  size_t index;

  for (index = 0; index < context->expansions.length; ++index) {
    free(context->expansions.items[index].path);
  }
  macro_source_ranges_delete(&context->macro_arguments);
  macro_source_ranges_delete(&context->macro_definitions);
  free(context->expansions.items);
}

static bool identifier_start(unsigned char character) {
  return character == '_' || (character >= 'A' && character <= 'Z') ||
         (character >= 'a' && character <= 'z');
}

static bool identifier_continue(unsigned char character) {
  return identifier_start(character) || (character >= '0' && character <= '9');
}

static bool c_keyword(const char *identifier) {
  static const char *const keywords[] = {
      "_Alignas",
      "_Alignof",
      "_Atomic",
      "_Bool",
      "_Complex",
      "_Generic",
      "_Imaginary",
      "_Noreturn",
      "_Static_assert",
      "_Thread_local",
      "alignas",
      "alignof",
      "auto",
      "bool",
      "break",
      "case",
      "char",
      "const",
      "constexpr",
      "continue",
      "default",
      "do",
      "double",
      "else",
      "enum",
      "extern",
      "false",
      "float",
      "for",
      "goto",
      "if",
      "inline",
      "int",
      "long",
      "nullptr",
      "register",
      "restrict",
      "return",
      "short",
      "signed",
      "sizeof",
      "static",
      "static_assert",
      "struct",
      "switch",
      "thread_local",
      "true",
      "typedef",
      "typeof",
      "typeof_unqual",
      "union",
      "unsigned",
      "void",
      "volatile",
      "while",
  };
  size_t index;

  for (index = 0; index < sizeof(keywords) / sizeof(keywords[0]); ++index) {
    if (strcmp(identifier, keywords[index]) == 0) {
      return true;
    }
  }
  return false;
}

static bool string_array_contains(char **items, size_t length,
                                  const char *value) {
  size_t index;
  for (index = 0; index < length; ++index) {
    if (strcmp(items[index], value) == 0) {
      return true;
    }
  }
  return false;
}

static bool identifier_is_member(const SourceFile *file, size_t range_start,
                                 size_t identifier_offset) {
  size_t cursor = identifier_offset;

  while (cursor > range_start &&
         (file->text[cursor - 1] == ' ' || file->text[cursor - 1] == '\t' ||
          file->text[cursor - 1] == '\r' || file->text[cursor - 1] == '\n')) {
    --cursor;
  }
  if (cursor > range_start && file->text[cursor - 1] == '.') {
    return true;
  }
  return cursor >= range_start + 2 && file->text[cursor - 1] == '>' &&
         file->text[cursor - 2] == '-';
}

static void scan_source_range(WalkContext *context, const char *path,
                              const MarkerRange *range, const char *function,
                              bool definition) {
  SourceFile *file = source_files_get(context->source_files, path);
  size_t start;
  size_t end;
  size_t cursor;
  unsigned line = range->start_line;
  char **parameters = NULL;
  size_t parameter_count = 0;
  size_t parameter_capacity = 0;
  bool first_identifier = true;
  bool signature_pending = false;
  bool in_parameters = false;
  unsigned parameter_depth = 0;

  if (file == NULL) {
    context->status = 1;
    return;
  }
  if (!source_coordinate_offset(file, range->start_line, range->start_column,
                                &start) ||
      !source_coordinate_offset(file, range->end_line, range->end_column,
                                &end) ||
      end < start) {
    fprintf(stderr, "cxref: invalid preprocessor source range for %s\n", path);
    context->status = 1;
    return;
  }

  cursor = start;
  while (cursor < end) {
    unsigned char character = (unsigned char)file->text[cursor];

    if (character == '\n') {
      ++line;
      ++cursor;
      continue;
    }
    if (character == '/' && cursor + 1 < end && file->text[cursor + 1] == '/') {
      cursor += 2;
      while (cursor < end && file->text[cursor] != '\n') {
        ++cursor;
      }
      continue;
    }
    if (character == '/' && cursor + 1 < end && file->text[cursor + 1] == '*') {
      cursor += 2;
      while (cursor < end && !(file->text[cursor] == '*' && cursor + 1 < end &&
                               file->text[cursor + 1] == '/')) {
        if (file->text[cursor++] == '\n') {
          ++line;
        }
      }
      if (cursor + 1 < end) {
        cursor += 2;
      }
      continue;
    }
    if (character == '\'' || character == '"') {
      unsigned char quote = character;
      ++cursor;
      while (cursor < end) {
        character = (unsigned char)file->text[cursor++];
        if (character == '\\' && cursor < end) {
          if (file->text[cursor] == '\n') {
            ++line;
          }
          ++cursor;
        } else if (character == '\n') {
          ++line;
        } else if (character == quote) {
          break;
        }
      }
      continue;
    }
    if (signature_pending && character == '(') {
      signature_pending = false;
      in_parameters = true;
      parameter_depth = 1;
      ++cursor;
      continue;
    }
    if (signature_pending) {
      signature_pending = false;
    }
    if (in_parameters && character == '(') {
      ++parameter_depth;
      ++cursor;
      continue;
    }
    if (in_parameters && character == ')') {
      if (--parameter_depth == 0) {
        in_parameters = false;
      }
      ++cursor;
      continue;
    }
    if (identifier_start(character)) {
      size_t identifier_offset = cursor;
      char *identifier;

      while (cursor < end &&
             identifier_continue((unsigned char)file->text[cursor])) {
        ++cursor;
      }
      identifier =
          copy_text(file->text + identifier_offset, cursor - identifier_offset);
      if (first_identifier) {
        first_identifier = false;
        signature_pending = definition;
        if (!definition) {
          reference_list_add(context->references, identifier, path, function,
                             line, false, false, context->translation_unit);
        }
      } else if (in_parameters) {
        if (!string_array_contains(parameters, parameter_count, identifier)) {
          if (parameter_count == parameter_capacity) {
            parameter_capacity =
                parameter_capacity == 0 ? 4 : parameter_capacity * 2;
            parameters = checked_realloc(parameters, parameter_capacity,
                                         sizeof(*parameters));
          }
          parameters[parameter_count++] = copy_string(identifier);
        }
      } else if (!c_keyword(identifier) &&
                 !string_array_contains(parameters, parameter_count,
                                        identifier) &&
                 !identifier_is_member(file, start, identifier_offset)) {
        Reference *reference =
            reference_list_add(context->references, identifier, path, function,
                               line, false, false, context->translation_unit);
        if (reference != NULL && !definition) {
          reference->macro_argument = true;
        } else if (reference != NULL) {
          reference->macro_replacement = true;
        }
      }
      free(identifier);
      continue;
    }
    ++cursor;
  }
  for (cursor = 0; cursor < parameter_count; ++cursor) {
    free(parameters[cursor]);
  }
  free(parameters);
}

static bool parse_bare_macro_marker(const char *text, char **name) {
  const char *cursor;
  const char *start;

  if (strncmp(text, "/*", 2) != 0) {
    return false;
  }
  cursor = text + 2;
  start = cursor;
  if (!identifier_start((unsigned char)*cursor)) {
    return false;
  }
  while (identifier_continue((unsigned char)*cursor)) {
    ++cursor;
  }
  if (strcmp(cursor, "*/") != 0) {
    return false;
  }
  *name = copy_text(start, (size_t)(cursor - start));
  return true;
}

static void add_macro_marker(WalkContext *context, TSNode node,
                             const char *function) {
  TSPoint point = ts_node_start_point(node);
  const char *path;
  char *text;
  MarkerRange range = {0};
  MacroSourceRange *argument;
  MacroSourceRange *definition;
  char *substitution_name = NULL;
  char *expansion_name = NULL;
  char *bare_name = NULL;

  if (point.row >= context->map->count ||
      context->map->paths[point.row] == NULL) {
    return;
  }
  path = context->map->paths[point.row];
  text = node_text(node, context->source);

  if (strcmp(text, "/*>*/") == 0) {
    expansion_frame_pop(context);
  } else if (parse_argument_marker(text, &range)) {
    const ExpansionFrame *inherited = NULL;
    ExpansionOrigin origin = EXPANSION_ORIGIN_ARGUMENT;
    if (range.start_line == 0) {
      /* MCPP omits coordinates when an enclosing replacement supplies the
       * nested argument. Skip the nested macro frame and inherit its caller. */
      inherited = enclosing_macro_expansion(context);
      origin = inherited == NULL ? EXPANSION_ORIGIN_NONE : inherited->origin;
      if (inherited != NULL) {
        range.start_line = inherited->start_line;
        range.start_column = 1;
        range.end_line = inherited->end_line;
        range.end_column = 1;
        path = inherited->path;
      }
    }
    macro_source_range_record(&context->macro_arguments, path, &range, origin);
  } else if (strncmp(text, "/*<", 3) == 0) {
    if (parse_marker_range(text, '<', &range)) {
      scan_source_range(context, path, &range, function, false);
      definition =
          macro_source_range_find(&context->macro_definitions, range.name);
      expansion_frame_push(context, definition);
      free(range.name);
    } else if (parse_substitution_marker(text, &substitution_name)) {
      argument =
          macro_source_range_find(&context->macro_arguments, substitution_name);
      expansion_frame_push(context, argument);
      free(substitution_name);
    } else if (parse_macro_expansion_marker(text, &expansion_name)) {
      definition =
          macro_source_range_find(&context->macro_definitions, expansion_name);
      expansion_frame_push(context, definition);
      free(expansion_name);
    } else {
      expansion_frame_push(context, NULL);
    }
  } else if (parse_marker_range(text, 'm', &range)) {
    reference_list_add(context->references, range.name, path, function,
                       range.start_line, true, false,
                       context->translation_unit);
    scan_source_range(context, path, &range, function, true);
    macro_source_range_record(&context->macro_definitions, path, &range,
                              EXPANSION_ORIGIN_DEFINITION);
  } else if (strncmp(text, "/*if", 4) == 0 || strncmp(text, "/*elif", 6) == 0) {
    context->condition_markers = true;
  } else if (strncmp(text, "/*i ", 4) == 0) {
    context->condition_markers = false;
  } else if (context->condition_markers &&
             parse_bare_macro_marker(text, &bare_name)) {
    reference_list_add(context->references, bare_name, path, function,
                       context->map->lines[point.row], false, false,
                       context->translation_unit);
    free(bare_name);
  }
  free(text);
}

static TSNode function_declarator_for_identifier(TSNode identifier) {
  TSNode child = identifier;
  bool pointer_declarator_seen = false;

  while (true) {
    TSNode parent = ts_node_parent(child);
    const char *type;
    const char *field;
    if (ts_node_is_null(parent)) {
      return (TSNode){0};
    }
    type = ts_node_type(parent);
    field = field_for_child(parent, child);
    if (strcmp(type, "function_declarator") == 0 && field != NULL &&
        strcmp(field, "declarator") == 0 && !pointer_declarator_seen) {
      return parent;
    }
    if (strcmp(type, "pointer_declarator") == 0 && field != NULL &&
        strcmp(field, "declarator") == 0) {
      pointer_declarator_seen = true;
    }
    child = parent;
  }
}

static void walk_tree(WalkContext *context, TSNode node, const char *function,
                      LexicalScope scope, TSNode definition_parameter_owner,
                      const LexicalScope *definition_parameter_scope) {
  const char *type = ts_node_type(node);
  char *owned_function = NULL;
  uint32_t index;

  if (strcmp(type, "function_definition") == 0) {
    TSNode declarator = ts_node_child_by_field_name(node, "declarator", 10);
    TSNode body = ts_node_child_by_field_name(node, "body", 4);
    TSNode identifier = first_identifier(declarator);
    TSNode parameter_owner = (TSNode){0};
    LexicalScope function_scope = lexical_scope_for_node(node, scope.depth + 1);
    if (!ts_node_is_null(identifier)) {
      owned_function = node_text(identifier, context->source);
      function = owned_function;
      parameter_owner = function_declarator_for_identifier(identifier);
    }
    for (index = 0; index < ts_node_named_child_count(node); ++index) {
      TSNode child = ts_node_named_child(node, index);
      if (!ts_node_is_null(body) && ts_node_eq(child, body)) {
        walk_tree(context, child, function, function_scope, (TSNode){0}, NULL);
      } else if (strcmp(ts_node_type(child), "declaration") == 0) {
        /* Old-style parameter declarations are direct function children. */
        walk_tree(context, child, function, function_scope, (TSNode){0}, NULL);
      } else {
        walk_tree(context, child, function, scope, parameter_owner,
                  &function_scope);
      }
    }
    free(owned_function);
    return;
  }
  if (strcmp(type, "function_declarator") == 0) {
    bool definition_parameters = !ts_node_is_null(definition_parameter_owner) &&
                                 ts_node_eq(node, definition_parameter_owner) &&
                                 definition_parameter_scope != NULL;
    for (index = 0; index < ts_node_named_child_count(node); ++index) {
      TSNode child = ts_node_named_child(node, index);
      const char *field = field_for_child(node, child);
      if (field != NULL && strcmp(field, "parameters") == 0) {
        LexicalScope parameter_scope =
            definition_parameters
                ? *definition_parameter_scope
                : lexical_scope_for_node(child, scope.depth + 1);
        walk_tree(context, child, function, parameter_scope, (TSNode){0}, NULL);
      } else {
        walk_tree(context, child, function, scope, definition_parameter_owner,
                  definition_parameter_scope);
      }
    }
    return;
  }
  if (strcmp(type, "compound_statement") == 0 ||
      strcmp(type, "for_statement") == 0) {
    scope = lexical_scope_for_node(node, scope.depth + 1);
  }
  if (strcmp(type, "identifier") == 0) {
    add_identifier(context, node, function, scope);
  } else if (strcmp(type, "comment") == 0) {
    add_macro_marker(context, node, function);
  }
  for (index = 0; index < ts_node_named_child_count(node); ++index) {
    walk_tree(context, ts_node_named_child(node, index), function, scope,
              definition_parameter_owner, definition_parameter_scope);
  }
}

static int analyze_file(const char *path, const char *encoding_option,
                        const PreprocessorOptions *options,
                        ReferenceList *references, size_t translation_unit) {
  char **arguments;
  char *absolute_path;
  char *preprocessed;
  char *parser_source;
  char *diagnostics;
  size_t argument_capacity = 4 + options->length;
  size_t argument_count;
  size_t index = 0;
  size_t option_index;
  int status;
  TSParser *parser;
  TSTree *tree;
  TSNode root;
  SourceMap map;
  SourceFiles source_files = {0};
  WalkContext context;

  absolute_path = absolute_operand(path);
  if (absolute_path == NULL) {
    return 1;
  }
  arguments = checked_realloc(NULL, argument_capacity + 1, sizeof(*arguments));
  arguments[index++] = copy_string("mcpp");
  arguments[index++] = copy_string("-K");
  arguments[index++] = copy_string(encoding_option);
  for (option_index = 0; option_index < options->length; ++option_index) {
    const PreprocessorOption *option = &options->items[option_index];
    const char *prefix;

    if (preprocessor_option_is_superseded(options, option_index)) {
      continue;
    }
    prefix = option->kind == PREPROCESSOR_DEFINE    ? "-D"
             : option->kind == PREPROCESSOR_INCLUDE ? "-I"
                                                    : "-U";
    arguments[index++] = join_option(
        prefix, option->kind == PREPROCESSOR_UNDEFINE ? option->name
                                                      : option->argument);
  }
  arguments[index++] = absolute_path;
  arguments[index] = NULL;
  argument_count = index;

  mcpp_use_mem_buffers(1);
  status = mcpp_lib_main((int)argument_count, arguments);
  preprocessed = mcpp_get_mem_buffer(OUT);
  diagnostics = mcpp_get_mem_buffer(ERR);
  if (diagnostics != NULL && diagnostics[0] != '\0') {
    fputs(diagnostics, stderr);
  }
  for (index = 0; index < argument_count; ++index) {
    free(arguments[index]);
  }
  free(arguments);
  if (status != 0 || preprocessed == NULL) {
    return status == 0 ? 1 : status;
  }

  parser = ts_parser_new();
  if (parser == NULL || !ts_parser_set_language(parser, tree_sitter_c())) {
    fprintf(stderr, "cxref: cannot initialize the C parser\n");
    ts_parser_delete(parser);
    return 1;
  }
  parser_source = parser_source_build(preprocessed);
  tree = ts_parser_parse_string(parser, NULL, parser_source,
                                (uint32_t)strlen(parser_source));
  if (tree == NULL) {
    fprintf(stderr, "cxref: cannot parse %s\n", path);
    free(parser_source);
    ts_parser_delete(parser);
    return 1;
  }

  root = ts_tree_root_node(tree);
  map = source_map_build(preprocessed, path);
  context = (WalkContext){
      .source = parser_source,
      .map = &map,
      .references = references,
      .source_files = &source_files,
      .translation_unit = translation_unit,
  };
  walk_tree(&context, root, NULL, lexical_scope_for_node(root, 0), (TSNode){0},
            NULL);
  if (ts_node_has_error(root)) {
    fprintf(stderr, "cxref: %s contains C syntax errors\n", path);
    status = 1;
  }
  if (context.status != 0) {
    status = 1;
  }

  macro_expansion_state_delete(&context);
  source_files_delete(&source_files);
  source_map_delete(&map);
  ts_tree_delete(tree);
  ts_parser_delete(parser);
  free(parser_source);
  return status;
}

static int compare_nullable(const char *left, const char *right) {
  const char *left_text = left == NULL ? "" : left;
  const char *right_text = right == NULL ? "" : right;
  int comparison = strcoll(left_text, right_text);
  return comparison != 0 ? comparison : strcmp(left_text, right_text);
}

static int reference_compare(const void *left_pointer,
                             const void *right_pointer) {
  const Reference *left = left_pointer;
  const Reference *right = right_pointer;
  int comparison = strcoll(left->name, right->name);
  if (comparison == 0) {
    comparison = strcmp(left->name, right->name);
  }
  if (comparison != 0) {
    return comparison;
  }
  comparison = strcoll(left->path, right->path);
  if (comparison == 0) {
    comparison = strcmp(left->path, right->path);
  }
  if (comparison != 0) {
    return comparison;
  }
  comparison = compare_nullable(left->function, right->function);
  if (comparison != 0) {
    return comparison;
  }
  if (left->line != right->line) {
    return left->line < right->line ? -1 : 1;
  }
  if (left->declaration != right->declaration) {
    return left->declaration ? -1 : 1;
  }
  return 0;
}

static bool reference_equal(const Reference *left, const Reference *right) {
  return strcmp(left->name, right->name) == 0 &&
         strcmp(left->path, right->path) == 0 &&
         ((left->function == NULL && right->function == NULL) ||
          (left->function != NULL && right->function != NULL &&
           strcmp(left->function, right->function) == 0)) &&
         left->line == right->line && left->declaration == right->declaration;
}

static bool reference_scope_contains(const Reference *declaration,
                                     const Reference *reference) {
  return declaration->scope.start_byte <= reference->position &&
         reference->position < declaration->scope.end_byte;
}

static bool function_declarations_share_identity(const Reference *left,
                                                 const Reference *right) {
  if (strcmp(left->name, right->name) != 0 ||
      left->linkage == SYMBOL_LINKAGE_NONE || left->linkage != right->linkage) {
    return false;
  }
  return left->linkage == SYMBOL_LINKAGE_EXTERNAL ||
         left->translation_unit == right->translation_unit;
}

static const Reference *visible_declaration(const ReferenceList *references,
                                            const Reference *reference) {
  const Reference *best = NULL;
  size_t index;

  for (index = 0; index < references->length; ++index) {
    const Reference *candidate = &references->items[index];
    if (!candidate->ordinary_identifier || !candidate->declaration ||
        candidate->translation_unit != reference->translation_unit ||
        candidate->position > reference->position ||
        strcmp(candidate->name, reference->name) != 0 ||
        !reference_scope_contains(candidate, reference)) {
      continue;
    }
    if (best == NULL || candidate->scope.depth > best->scope.depth ||
        (candidate->scope.depth == best->scope.depth &&
         candidate->position > best->position)) {
      best = candidate;
    }
  }
  return best;
}

static const Reference *
external_function_declaration(const ReferenceList *references,
                              const Reference *reference) {
  size_t index;

  for (index = 0; index < references->length; ++index) {
    const Reference *candidate = &references->items[index];
    if (candidate->ordinary_identifier && candidate->declares_function &&
        candidate->linkage == SYMBOL_LINKAGE_EXTERNAL &&
        candidate->scope.depth == 0 &&
        candidate->translation_unit != reference->translation_unit &&
        strcmp(candidate->name, reference->name) == 0) {
      return candidate;
    }
  }
  return NULL;
}

static void resolve_function_symbols(ReferenceList *references) {
  size_t next_identity = 1;
  size_t index;

  for (index = 0; index < references->length; ++index) {
    Reference *declaration = &references->items[index];
    size_t candidate;

    if (!declaration->ordinary_identifier || !declaration->declares_function) {
      continue;
    }
    for (candidate = 0; candidate < index; ++candidate) {
      const Reference *prior = &references->items[candidate];
      if (prior->declares_function &&
          function_declarations_share_identity(prior, declaration)) {
        declaration->symbol_identity = prior->symbol_identity;
        break;
      }
    }
    if (declaration->symbol_identity == 0) {
      declaration->symbol_identity = next_identity++;
    }
  }

  for (index = 0; index < references->length; ++index) {
    Reference *reference = &references->items[index];
    const Reference *declaration;

    if (!reference->ordinary_identifier || reference->declaration) {
      continue;
    }
    reference->function_symbol = false;
    reference->linkage = SYMBOL_LINKAGE_NONE;
    reference->symbol_identity = 0;
    declaration = visible_declaration(references, reference);
    if (declaration == NULL) {
      declaration = external_function_declaration(references, reference);
    }
    if (declaration != NULL && declaration->declares_function) {
      reference->function_symbol = true;
      reference->linkage = declaration->linkage;
      reference->symbol_identity = declaration->symbol_identity;
    }
  }
}

static size_t next_wrapped_bytes(const char *text, unsigned width) {
  mbstate_t state = {0};
  size_t offset = 0;
  unsigned columns = 0;

  while (text[offset] != '\0') {
    wchar_t character;
    size_t length = mbrtowc(&character, text + offset, MB_CUR_MAX, &state);
    int character_width;
    if (length == (size_t)-1 || length == (size_t)-2) {
      memset(&state, 0, sizeof(state));
      length = 1;
      character_width = 1;
    } else if (length == 0) {
      break;
    } else {
      character_width = wcwidth(character);
      if (character_width < 0) {
        character_width = 1;
      }
    }
    if (columns != 0 && columns + (unsigned)character_width > width) {
      break;
    }
    columns += (unsigned)character_width;
    offset += length;
  }
  return offset == 0 ? 1 : offset;
}

static int emit_wrapped(FILE *output, const char *text, unsigned width) {
  const char *cursor = text;
  while (*cursor != '\0') {
    size_t length = next_wrapped_bytes(cursor, width);
    if (fwrite(cursor, 1, length, output) != length ||
        fputc('\n', output) == EOF) {
      return 1;
    }
    cursor += length;
  }
  if (text[0] == '\0' && fputc('\n', output) == EOF) {
    return 1;
  }
  return 0;
}

static int emit_references(FILE *output, ReferenceList *references,
                           unsigned width) {
  size_t index;
  size_t output_index = 0;
  int result = 0;

  resolve_function_symbols(references);
  qsort(references->items, references->length, sizeof(*references->items),
        reference_compare);
  for (index = 0; index < references->length; ++index) {
    Reference *reference = &references->items[index];
    const char *function =
        reference->function_symbol || reference->function == NULL
            ? "-"
            : reference->function;
    char line_text[32];
    char *logical;
    size_t logical_length;

    if (output_index > 0 &&
        reference_equal(reference, &references->items[output_index - 1])) {
      free(reference->name);
      free(reference->path);
      free(reference->function);
      continue;
    }
    if (output_index != index) {
      references->items[output_index] = *reference;
    }
    reference = &references->items[output_index++];
    snprintf(line_text, sizeof(line_text), "%s%u",
             reference->declaration ? "*" : "", reference->line);
    logical_length = strlen(reference->name) + strlen(reference->path) +
                     strlen(function) + strlen(line_text) + 10;
    logical = checked_realloc(NULL, logical_length, 1);
    snprintf(logical, logical_length, "%s | %s | %s | %s", reference->name,
             reference->path, function, line_text);
    result |= emit_wrapped(output, logical, width);
    free(logical);
  }
  references->length = output_index;
  return result;
}

static void usage(FILE *stream) {
  fputs("usage: cxref [-cs] [-o file] [-w num] [-D name[=def]] "
        "[-I dir] [-U name] file...\n",
        stream);
}

int main(int argc, char **argv) {
  bool combined = false;
  bool silent = false;
  const char *output_path = NULL;
  unsigned width = 80;
  char *encoding_option;
  PreprocessorOptions preprocessor_options = {0};
  ReferenceList combined_references = {0};
  FILE *output = stdout;
  int option;
  int status = 0;
  int file_index;

  setlocale(LC_ALL, "");
  encoding_option = preprocessor_encoding_option();
  opterr = 0;
  while ((option = getopt(argc, argv, "csD:I:U:o:w:")) != -1) {
    switch (option) {
    case 'c':
      combined = true;
      break;
    case 's':
      silent = true;
      break;
    case 'D':
      preprocessor_options_add(&preprocessor_options, PREPROCESSOR_DEFINE,
                               optarg);
      break;
    case 'I':
      preprocessor_options_add(&preprocessor_options, PREPROCESSOR_INCLUDE,
                               optarg);
      break;
    case 'U':
      if (strchr(optarg, '=') != NULL) {
        fprintf(stderr, "cxref: invalid macro name for -U: %s\n", optarg);
        status = 2;
        goto done;
      }
      preprocessor_options_add(&preprocessor_options, PREPROCESSOR_UNDEFINE,
                               optarg);
      break;
    case 'o':
      output_path = optarg;
      break;
    case 'w': {
      char *end;
      unsigned long parsed;
      errno = 0;
      parsed = strtoul(optarg, &end, 10);
      if (errno != 0 || *optarg == '\0' || *end != '\0' ||
          parsed > UINT32_MAX) {
        fprintf(stderr, "cxref: invalid width: %s\n", optarg);
        status = 2;
        goto done;
      }
      width = parsed < 51 ? 80 : (unsigned)parsed;
      break;
    }
    default:
      usage(stderr);
      status = 2;
      goto done;
    }
  }
  if (optind == argc) {
    usage(stderr);
    status = 2;
    goto done;
  }
  for (file_index = optind; file_index < argc; ++file_index) {
    if (strchr(argv[file_index], '\n') != NULL) {
      fprintf(stderr, "cxref: input pathname contains a newline\n");
      status = 2;
      goto done;
    }
  }
  if (output_path != NULL) {
    output = fopen(output_path, "w");
    if (output == NULL) {
      fprintf(stderr, "cxref: %s: %s\n", output_path, strerror(errno));
      status = 1;
      goto done;
    }
  }

  for (file_index = optind; file_index < argc; ++file_index) {
    ReferenceList references = {0};
    int file_status =
        analyze_file(argv[file_index], encoding_option, &preprocessor_options,
                     &references, (size_t)(file_index - optind + 1));
    if (file_status != 0) {
      status = 1;
    }
    if (combined) {
      reference_list_append(&combined_references, &references);
    } else {
      if (!silent) {
        status |= emit_wrapped(output, argv[file_index], width);
      }
      status |= emit_references(output, &references, width);
      reference_list_delete(&references);
    }
  }
  if (combined) {
    status |= emit_references(output, &combined_references, width);
  }
  if (fflush(output) != 0) {
    fprintf(stderr, "cxref: output: %s\n", strerror(errno));
    status = 1;
  }

done:
  if (output != stdout && fclose(output) != 0) {
    fprintf(stderr, "cxref: output: %s\n", strerror(errno));
    status = 1;
  }
  reference_list_delete(&combined_references);
  preprocessor_options_delete(&preprocessor_options);
  free(encoding_option);
  return status;
}
