require_relative 'constraints'

module ActiveRecord
  class Schema
    # This method is responsible for defining the database schema
    def self.define(info = {}, &block)
      # The `info` hash can include metadata like the schema version
      version = info[:version]

      # The `DSL` class is responsible for interpreting the block and applying it to the database schema
      dsl = DSL.new
      dsl.instance_eval(&block)
      return dsl.tables
    end

    # This inner class acts as a Domain-Specific Language (DSL) for schema definition
    class DSL
      attr_reader :tables

      def initialize
        @tables = {}
        @current_table = nil
      end

      def create_table(name, options = {}, &block)
        @current_table = name.to_sym
        @tables[@current_table] = { columns: {}, indices: [], foreign_keys: [], constraints: []}
        yield(self) if block_given?
      end

      def index(table, columns, options = {})
        @tables[@current_table][:indices] << { columns: Array(columns), options: options }
      end

      def add_foreign_key(from_table, to_table, options = {})
        @tables[from_table.to_sym][:foreign_keys] << { to_table: to_table, options: options }
      end

      def method_missing(method_name, *args, &block)
        if @current_table
          column_name = args.shift
          args.each do |h|
            h.each do |(key, value)|
              case key
              when :null
                @tables[@current_table][:constraints] << NonNullConstraint.new(@current_table.to_s, column_name.to_s, value)
              when :limit
                @tables[@current_table][:constraints] << LengthConstraint.new(@current_table.to_s, column_name.to_s, nil, value)
              end
            end
          end
          @tables[@current_table][:columns][column_name.to_sym] = { type: method_name, options: args.first || {} }
        end
      end
    end
  end
end

class SchemaParser
  attr_reader :tables

  def initialize
    @tables = {}
    @current_table = nil
  end

  def parse(schema_file)
    content = File.read(schema_file)
    instance_eval(content)
  end
end
