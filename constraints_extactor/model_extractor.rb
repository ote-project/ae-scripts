require 'ripper'
require 'pp'
require_relative 'constraints'

class ModelParser
  attr_accessor :table, :constraints
  def initialize(table_name)
    @table = table_name
    @constraints = []
  end

  def gen_constraint(ident, column, value)
    if ident.include?("presence")
      constraints << NonNullConstraint.new(@table, column, value)
    elsif ident.include?("uniqueness")
      constraints << UniqueConstraint.new(@table, column, false) # default to false for now
    elsif ident.include?("inclusion")
      constraints << InclusionConstraint.new(@table, column, value)
    elsif ident.include?("allow_blank")
      constraints << NonNullConstraint.new(@table, column, value == "true" ? "false" : "true")
    else
      puts "Constraint: #{ident} not implemented yet"
      exit
    end
  end

  def parse(model_file)
    content = File.read(model_file)
    root = Ripper.sexp(content)
    File.open("constraints", "w") do |file|
      self.traverse(root)
    end
    return constraints
  end

  def traverse(node)
    if node.is_a?(Array)
      if node.include?(:command)
        parse_command(node)
      else
        node.each do |child|
          traverse(child)
        end
      end
    elsif node.is_a?(Symbol)
      # Ignore symbols
    else
      # What else might be here
    end
  end

  def parse_command(node)
    if node.length != 3
      exit
    end

    sym = node[0]
    type = node[1]
    com_args = node[2]

    if type.include?("validates") and com_args.is_a?(Array)
      parse_validates(com_args)
    end
  end

  def parse_validates(com_args)
    if com_args.length != 3;
      puts "got validates with not 3 elements"
      pp com_args
      exit
    end

    sym, args = com_args[1]
    sym = get_sym(sym)
    _ = args.shift
    args = args[0]
    args.each do |item|
      if item.include?(:assoc_new)
        item.shift
        ident, value = parse_assoc_new(item)
        gen_constraint(ident, sym, value)
      end
    end
  end

  def parse_assoc_new(data)
    ident = nil
    value = nil
    data.each do |item|
      if item.is_a?(Array) and item.length == 1 # peel items
        item = item[0]
      end

      if item.include?(:symbol_literal) or item.include?(:symbol)
        ident = get_sym(item)
      elsif item.include?(:var_ref)
        value = item[1][1] # don't need a function for this
      elsif item.include?(:@label)
        ident = item[1]
      elsif item.include?(:hash)
        value = parse_hash(item)
      end
    end
    return ident.to_s, value.to_s
  end

  def parse_hash(hash)
    h = {}
    assoclist = hash[1][1]
    assoclist.each do |item|
      item.shift
      key, value = parse_assoc_new(item) # FIXME
      h[key] = value
    end
    return h
  end

  def get_sym(sym_lit)
    if sym_lit.include?(:symbol_literal)
      sym_lit.shift
      return get_sym(sym_lit)
    elsif sym_lit.include?(:symbol)
      sym_lit.shift
      return get_sym(sym_lit)
    elsif sym_lit.include?(:@ident)
      idx = sym_lit.index(:@ident)
      return sym_lit[idx+1]
    elsif sym_lit.is_a?(Array) and sym_lit.length == 1
      return get_sym(sym_lit[0])
    else
      puts "get_sym failed"
      pp sym_lit
      exit
    end
  end

end
