# frozen_string_literal: true
# require 'pp'

namespace :constraints do
  desc 'Extracts and documents all database constraints from a Rails application'

  task extract: :environment do
    class ConstraintExtractor
      STRING_LIKE_TYPES = %i[string text]
      HAS_MACROS = %i[has_one has_many]

      def initialize
        @conn = ActiveRecord::Base.connection
        Rails.application.eager_load!
        @models = ActiveRecord::Base.descendants.select { |m| m.table_name.present? }
        @table_names = @models.map(&:table_name).uniq
      end

      def extract_all
        puts 'constraints = ['

        extract_primary_keys
        extract_unique_constraints
        extract_foreign_keys
        extract_timestamp_constraints
        extract_enum_constraints
        extract_presence_constraints
        # extract_sti_constraints

        puts ']'
      end

      private

      def extract_primary_keys
        puts '// Primary keys.'
        @table_names.each do |table_name|
          primary_key = @conn.primary_key(table_name)
          print_constraint("unique", table_name, [primary_key])
        end
        puts
      end

      def extract_unique_constraints
        puts '// Uniqueness constraints from indexes and validations.'
        extract_unique_indexes
        extract_uniqueness_validations
        puts
      end

      def extract_unique_indexes
        @table_names.each do |table_name|
          @conn.indexes(table_name).select(&:unique).each do |index|
            print_constraint("unique", table_name, index.columns)
          end
        end
      end

      def extract_uniqueness_validations
        @models.each do |model|
          process_uniqueness_validators(model)
        end
      end

      def extract_timestamp_constraints
        puts '// Timestamp NOT NULL constraints'
        @table_names.each do |table_name|
          columns = @conn.columns(table_name)
          print_non_null(table_name, 'created_at') if columns.any? { |c| c.name == 'created_at' }
          print_non_null(table_name, 'updated_at') if columns.any? { |c| c.name == 'updated_at' }
        end
        puts
      end

      def extract_enum_constraints
        puts '// Enum constraints'
        @models.each do |model|
          table_name = model.table_name
          model.defined_enums.each do |attr, defs|
            next unless defs.is_a?(Hash)

            column = model.columns_hash[attr.to_s]
            next unless column

            values = defs.values
            next unless values.all?(Integer)

            puts "{ type = \"one-of-int\", tbl = \"#{table_name}\", col = \"#{column.name}\", allowed-values = [#{values.join(', ')}] },"
          end
        end
        puts
      end

      def extract_presence_constraints
        puts '// Presence and numericality constraints'
        @models.each do |model|
          extract_model_presence_constraints(model)
          # extract_model_numericality_constraints(model) # FIXME(kerneyj) when I look for ActiveRecord::Validations::NumericalityValidator, that class does not exist
        end
        puts
      end

      def extract_model_presence_constraints(model)
        model.validators.each do |validator|
          next unless validator.is_a?(ActiveRecord::Validations::PresenceValidator)
          options = validator.options.dup
          attribute = validator.attributes.first
          column = model.columns_hash[attribute.to_s]
          next unless column
          puts "{ type = \"non-null\", tbl = \"#{model.table_name}\", col = \"#{column.name}\" }" # TODO(kerneyj) I'm not sure that presence == non-null, I think non-null is a subset of presence
          # FIXME(kerneyj) So there is actually a lot more to the above, the real question is how to support spaces in the string encoding for Z3
        end
      end

      def extract_model_numericality_constraints(model)
        model.validators.each do |validator|
          next unless validator.is_a?(ActiveRecord::Validations::NumericalityValidator)
          options = validator.options.dup
          pp options
        end
      end

      def extract_foreign_keys
        puts '// Foreign keys.'
        @models.each do |model|
          from_tbl = model.table_name
          model.reflect_on_all_associations(:belongs_to).each do |association|
            from_col = association.foreign_key

            if association.polymorphic?
              handle_polymorphic_association(from_tbl, from_col, association)
            else
              handle_standard_association(from_tbl, from_col, association)
            end
          end
        end
        puts
      end

      def handle_sti_association(from_tbl, from_col, to_tbl, to_col, to_klass, is_optional)
        to_type = to_klass.sti_name
        print_query_is_set_contained_in(
          "SELECT `#{from_col}` FROM `#{from_tbl}`",
          "SELECT `#{to_col}` FROM `#{to_tbl}` WHERE `#{to_klass.inheritance_column}` = '#{to_type}'"
        )
        print_non_null(from_tbl, from_col) unless is_optional
      end

      def handle_standard_association(from_tbl, from_col, association)
        begin
          to_klass = association.klass
          to_tbl = to_klass.table_name
        rescue NameError => e
          puts "// Error: #{e}"
          return
        end

        to_col = association.association_primary_key
        is_optional = association.options[:optional]

        if to_klass.has_attribute?(to_klass.inheritance_column) && to_klass.descendants.empty?
          handle_sti_association(from_tbl, from_col, to_tbl, to_col, to_klass, is_optional)
        else
          type = is_optional ? 'foreign-key' : 'foreign-key-non-null'
          puts "{ type = \"#{type}\", from-tbl = \"#{from_tbl}\", from-col = \"#{from_col}\", to-tbl = \"#{to_tbl}\", to-col = \"#{to_col}\" },"
        end
      end

      def handle_polymorphic_association(from_tbl, from_col, association)
        return if association.options[:optional]

        from_type_col = association.foreign_type
        print_non_null(from_tbl, from_type_col)

        inverses = find_polymorphic_inverses(association)
        return if inverses.empty?

        all_type_names = inverses.map { |a| a.active_record.name }
        print_one_of_string(from_tbl, from_type_col, all_type_names)

        if inverses.length == 1
          handle_single_inverse(from_tbl, from_col, inverses.first)
        else
          handle_multiple_inverses(from_tbl, from_col, from_type_col, inverses)
        end
      end

      def find_polymorphic_inverses(association)
        @models.flat_map(&:reflect_on_all_associations).select do |a|
          HAS_MACROS.include?(a.macro) &&
          a.options[:as] == association.name &&
          a.klass == association.active_record
        end
      end

      def handle_single_inverse(from_tbl, from_col, inverse)
        to_tbl = inverse.active_record.table_name
        to_col = inverse.join_foreign_key
        puts "{ type = \"foreign-key-non-null\", from-tbl = \"#{from_tbl}\", from-col = \"#{from_col}\", to-tbl = \"#{to_tbl}\", to-col = \"#{to_col}\" },"
      end

      def handle_multiple_inverses(from_tbl, from_col, from_type_col, inverses)
        inverses.each do |inverse|
          type_name = inverse.active_record.name
          to_tbl = inverse.active_record.table_name
          to_col = inverse.join_foreign_key
          print_query_is_set_contained_in(
            "SELECT `#{from_col}` FROM `#{from_tbl}` WHERE `#{from_type_col}` = '#{type_name}'",
            "SELECT `#{to_col}` FROM `#{to_tbl}`"
          )
        end
      end

      def process_uniqueness_validators(model)
        model.validators.each do |validator|
          next unless validator.is_a?(ActiveRecord::Validations::UniquenessValidator)

          options = validator.options.dup
          options.delete(:allow_blank)
          allow_nil = options.delete(:allow_nil)
          scope = Array(options.delete(:scope))

          next unless options.delete(:case_sensitive)
          next unless options.empty? # Skip if there are unsupported options
          next unless validator.attributes.size == 1

          attribute = validator.attributes.first
          column = model.columns_hash[attribute.to_s]
          next unless column

          uniq_col_names = [column.name] + scope.map(&:to_s)
          print_constraint("unique", model.table_name, uniq_col_names)

          # Add NOT NULL constraint if allow_nil is false
          print_non_null(model.table_name, column.name) unless allow_nil
        end
      end

      def print_constraint(type, table, columns)
        column_list = columns.map { |c| "\"#{c}\"" }.join(', ')
        puts "{ type = \"#{type}\", tbl = \"#{table}\", cols = [#{column_list}] },"
      end

      def print_non_null(tbl, col)
        puts "{ type = \"non-null\", tbl = \"#{tbl}\", col = \"#{col}\" },"
      end

      def print_one_of_string(tbl, col, allowed_values)
        allowed_values = allowed_values.uniq
        values_list = allowed_values.map { |v| "\"#{v}\"" }.join(', ')
        puts "{ type = \"one-of-string\", tbl = \"#{tbl}\", col = \"#{col}\", allowed-values = [#{values_list}] },"
      end

      def print_query_is_set_contained_in(sql1, sql2)
        puts "{ type = \"query-is-set-contained-in\", sql-1 = \"#{sql1}\", sql-2 = \"#{sql2}\" },"
      end
    end

    ConstraintExtractor.new.extract_all
  end
end
