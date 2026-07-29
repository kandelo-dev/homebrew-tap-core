# typed: strict
# frozen_string_literal: true

# One source of truth for Erlang's wasm32 optimizer-boundary smoke test.
#
# WHY: BEAM startup is expensive in Formula validation, and separate Node and
# Chromium snippets had drifted into shallow checks. This module builds one
# bounded Erlang program for each host and validates a line protocol strictly,
# so every reviewed semantic case must run in both hosts before publication.
# See Kandelo/erlang-wasm32-optimizer-workarounds.md.
module KandeloErlangWasm32Smoke
  class SmokeFailure < RuntimeError; end

  ORACLE = "native Erlang/OTP 28; ERTS 16.4; 2026-07-28"
  CASE_KEYS = [:name, :exercises, :why, :expression, :expected].freeze
  CASES = [
    {
      name:       "term_to_binary_roundtrip",
      exercises:  "external.c enc_term/dec_term WSTACK",
      why:        "a 500-deep tuple exceeds the 16-entry inline stack",
      expression: <<~ERLANG.strip,
        T = lists:foldl(fun(I,A) -> {I,A} end, nil, lists:seq(1,500)),
        {binary_to_term(term_to_binary(T)) =:= T, byte_size(term_to_binary(T))}
      ERLANG
      expected:   "{true,2741}",
    },
    {
      name:       "unicode_deep",
      exercises:  "erl_unicode.c iodata ESTACK",
      why:        "50 mixed Unicode fragments exceed the 16-entry inline stack",
      expression: <<~ERLANG.strip,
        L = lists:duplicate(50, [16#1F600, <<"héllo"/utf8>>, "abc"]),
        B = unicode:characters_to_binary(L),
        {byte_size(B), lists:sum(binary_to_list(B))}
      ERLANG
      expected:   "{650,88400}",
    },
    {
      name:       "chksum_iolist",
      exercises:  "erl_bif_chksum.c checksum iodata ESTACK",
      why:        "100 nested mixed fragments exercise direct, seeded, and incremental checksum APIs",
      expression: <<~ERLANG.strip,
        IO = lists:duplicate(100, [<<"kan">>, "de", [108,111]]),
        {Left, Right} = lists:split(50, IO),
        IncrementalMd5 = erlang:md5_final(
          erlang:md5_update(erlang:md5_update(erlang:md5_init(), Left), Right)
        ),
        DirectMd5 = erlang:md5(IO),
        {byte_size(iolist_to_binary(IO)), IncrementalMd5 =:= DirectMd5, DirectMd5,
         erlang:crc32(IO), erlang:crc32(123456789, IO),
         erlang:adler32(IO), erlang:adler32(123456789, IO)}
      ERLANG
      expected:   "{700,true,<<140,120,254,181,217,34,17,213,230,74,169,247,123,44,26,193>>," \
                  "2948946130,3774254194,2665225928,2311515100}",
    },
    {
      name:       "ets_match",
      exercises:  "erl_db_util.c and erl_db_hash.c DMC stack",
      why:        "a 100-row match traversal crosses the inline stack boundary",
      expression: <<~ERLANG.strip,
        T = ets:new(kandelo_tap_ets, [bag]),
        [ets:insert(T, {K, K*K, "v"}) || K <- lists:seq(1,100)],
        R = ets:select(T, [{{'$1','$2','_'}, [{'>','$2',2500}], ['$1']}]),
        ets:delete(T),
        {length(R), lists:sum(R), lists:min(R), lists:max(R)}
      ERLANG
      expected:   "{50,3775,51,100}",
    },
    {
      name:       "term_compare_sort",
      exercises:  "utils.c comparison ESTACK and erl_map.c WSTACK",
      why:        "100 heterogeneous deep terms drive comparison past 16 entries",
      expression: <<~'ERLANG'.strip,
        L = [{I rem 7, #{a => I, b => lists:seq(1, I rem 20)},
              [I | lists:seq(1, I rem 25)]} || I <- lists:seq(1,100)],
        S = lists:sort(L),
        {S =:= lists:sort(lists:reverse(L)), erlang:phash2(S)}
      ERLANG
      expected:   "{true,45350027}",
    },
    {
      name:       "phash2_deep",
      exercises:  "erl_term_hashing.c WSTACK",
      why:        "a 300-deep tuple exceeds the 16-entry inline stack",
      expression: <<~ERLANG.strip,
        T = lists:foldl(fun(I,A) -> {I,A} end, done, lists:seq(1,300)),
        erlang:phash2(T)
      ERLANG
      expected:   "64685795",
    },
    {
      name:       "copy_large_term",
      exercises:  "copy.c copy_struct and size_object ESTACK",
      why:        "a 400-deep term is copied through process messaging",
      expression: <<~ERLANG.strip,
        T = lists:foldl(fun(I,A) -> {I,A} end, nil, lists:seq(1,400)),
        Sender = self(),
        Peer = spawn(fun() -> receive X -> Sender ! erlang:phash2(X) end end),
        Peer ! T,
        H = receive R -> R after 5000 -> timeout end,
        {H =:= erlang:phash2(T), H}
      ERLANG
      expected:   "{true,60113841}",
    },
    {
      name:       "format_p_deep",
      exercises:  "erl_printf_term.c term-printer stack",
      why:        "printing a 100-element list crosses the inline stack boundary",
      expression: <<~ERLANG.strip,
        Str = lists:flatten(io_lib:format("~w", [lists:seq(1,100)])),
        {length(Str), erlang:phash2(Str)}
      ERLANG
      expected:   "{293,100105088}",
    },
    {
      name:       "iolist_to_binary_deep",
      exercises:  "erl_iolist.c EQUEUE",
      why:        "a 100-deep nested iolist exceeds the inline queue",
      expression: <<~ERLANG.strip,
        L = lists:foldl(fun(I,A) -> [<<I:8>>, A, "x"] end, [], lists:seq(1,100)),
        B = iolist_to_binary(L),
        {byte_size(B), binary:first(B), binary:last(B), erlang:iolist_size(L)}
      ERLANG
      expected:   "{200,100,120,200}",
    },
    {
      name:       "compile_module",
      exercises:  "compiler forms path and beam_asm checksum traversal",
      why:        "an in-guest compiler run reaches beam_asm's checksum iodata path",
      expression: <<~ERLANG.strip,
        Scan = fun(Str) ->
          {ok, Tokens, _} = erl_scan:string(Str),
          {ok, Form} = erl_parse:parse_form(Tokens),
          Form
        end,
        Forms = [Scan("-module(kandelo_tap_forms)."),
                 Scan("-export([f/1])."),
                 Scan("f(N) -> lists:sum(lists:seq(1,N)).")],
        {ok, kandelo_tap_forms, Bin} = compile:forms(Forms, [binary]),
        {module, kandelo_tap_forms} =
          code:load_binary(kandelo_tap_forms, "kandelo_tap_forms.beam", Bin),
        {kandelo_tap_forms:f(100), is_binary(Bin), byte_size(Bin) > 100}
      ERLANG
      expected:   "{5050,true,true}",
    },
    {
      name:       "compile_file",
      exercises:  "compiler file path, guest VFS, parser, and beam_asm checksum traversal",
      why:        "a real source file proves the compiler closure is usable beyond compile:forms",
      expression: <<~'ERLANG'.strip,
        SourcePath = "/tmp/kandelo_tap_file.erl",
        FileResult = try
          ok = file:write_file(
            SourcePath,
            <<"-module(kandelo_tap_file).\n"
              "-export([f/1]).\n"
              "f(N) -> lists:sum(lists:seq(1,N)).\n">>
          ),
          {ok, kandelo_tap_file, FileBin} = compile:file(SourcePath, [binary]),
          {module, kandelo_tap_file} =
            code:load_binary(kandelo_tap_file, "kandelo_tap_file.beam", FileBin),
          {kandelo_tap_file:f(200), is_binary(FileBin), byte_size(FileBin) > 100}
        after
          _ = file:delete(SourcePath)
        end,
        FileResult
      ERLANG
      expected:   "{20100,true,true}",
    },
  ].map do |smoke_case|
    smoke_case.each_value(&:freeze)
    smoke_case.freeze
  end.freeze

  module_function

  def program(compiler_ebin:, cases: CASES)
    validate_cases!(cases)
    valid_compiler_ebin =
      compiler_ebin.is_a?(String) &&
      compiler_ebin.bytesize.between?(1, 4096) &&
      compiler_ebin.start_with?("/") &&
      File.expand_path(compiler_ebin) == compiler_ebin &&
      compiler_ebin.match?(%r{\A[/A-Za-z0-9._+-]+\z})
    raise ArgumentError, "compiler ebin must be a normalized absolute guest path" unless valid_compiler_ebin

    lines = [
      "case code:add_pathz(#{erlang_string(compiler_ebin)}) of",
      "  true -> ok;",
      "  CompilerPathError -> erlang:error({compiler_ebin, CompilerPathError})",
      "end,",
      "Run = fun(Name, Fun, Expected) ->",
      "  Got = try lists:flatten(io_lib:format(\"~w\", [Fun()]))",
      "        catch Class:Reason ->",
      "          lists:flatten(io_lib:format(\"caught_~w_~w\", [Class, Reason]))",
      "        end,",
      "  case Got =:= Expected of",
      "    true -> io:format(\"ok ~s~n\", [Name]);",
      "    false -> io:format(\"FAIL ~s expected=~s got=~s~n\", [Name, Expected, Got])",
      "  end",
      "end,",
    ]
    cases.each do |smoke_case|
      lines << "Run(#{erlang_string(smoke_case.fetch(:name))}, fun() ->"
      lines << smoke_case.fetch(:expression)
      lines << "end, #{erlang_string(smoke_case.fetch(:expected))}),"
    end
    lines << "io:format(\"matrix_done ~w~n\", [#{cases.length}]),"
    lines << "halt()."
    lines.join("\n")
  end

  def validate_output!(output, cases: CASES)
    validate_cases!(cases)
    raise SmokeFailure, "Erlang smoke output must be a string" unless output.is_a?(String)

    expected_names = cases.map { |smoke_case| smoke_case.fetch(:name) }
    observed = []
    failures = []
    sentinels = []
    malformed = []

    output.each_line do |raw_line|
      line = raw_line.strip
      case line
      when /\Aok (\S+)\z/
        observed << Regexp.last_match(1)
      when /\AFAIL (\S+)(?: .*)?\z/
        failures << line
      when /\Amatrix_done (\d+)\z/
        sentinels << Regexp.last_match(1).to_i
      else
        malformed << line if line.start_with?("ok ", "FAIL ", "matrix_done ")
      end
    end

    observed_counts = observed.tally
    duplicates = observed_counts.reject { |_name, count| count == 1 }.keys.sort
    missing = expected_names - observed
    unexpected = observed.uniq - expected_names
    problems = []
    problems << "failures=#{failures.join(" | ")}" unless failures.empty?
    problems << "missing=#{missing.sort.join(",")}" unless missing.empty?
    problems << "unexpected=#{unexpected.sort.join(",")}" unless unexpected.empty?
    problems << "duplicates=#{duplicates.join(",")}" unless duplicates.empty?
    problems << "malformed=#{malformed.join(" | ")}" unless malformed.empty?
    problems << "sentinels=#{sentinels.inspect}, expected=[#{cases.length}]" if sentinels != [cases.length]
    raise SmokeFailure, "Erlang wasm32 smoke matrix rejected output: #{problems.join("; ")}" unless problems.empty?

    true
  end

  def validate_cases!(cases)
    valid_cases = cases.is_a?(Array) && !cases.empty?
    raise ArgumentError, "Erlang smoke matrix must contain at least one case" unless valid_cases

    names = cases.map do |smoke_case|
      valid_case =
        smoke_case.is_a?(Hash) &&
        CASE_KEYS.all? { |key| smoke_case[key].is_a?(String) }
      raise ArgumentError, "Erlang smoke cases must define string metadata and oracle fields" unless valid_case

      name = smoke_case.fetch(:name)
      raise ArgumentError, "invalid Erlang smoke case name: #{name.inspect}" unless name.match?(/\A[a-z][a-z0-9_]*\z/)

      name
    end
    return if names.uniq.length == names.length

    raise ArgumentError, "Erlang smoke case names must be unique"
  end
  private_class_method :validate_cases!

  def erlang_string(value)
    %Q("#{value.gsub("\\", "\\\\\\\\").gsub('"', '\\"')}")
  end
  private_class_method :erlang_string
end
