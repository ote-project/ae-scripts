# frozen_string_literal: true
require 'pp'

namespace :constraints do
  desc 'Extracts and documents all database constraints from a Rails application'

  task extract: :environment do
    class ConstraintExtractor
      STRING_LIKE_TYPES = %i[string text]
      HAS_MACROS = %i[has_one has_many]

      def initialize # ✓
        @conn = ActiveRecord::Base.connection
        Rails.application.eager_load!
        @models = ActiveRecord::Base.descendants.select { |m| m.table_name.present? }
        @table_names = @models.map(&:table_name).uniq
      end

      def extract_all # ✓
        puts 'constraints = ['

        extract_primary_keys
        extract_unique_constraints
        extract_foreign_keys
        # extract_timestamp_constraints
        extract_enum_constraints
        extract_presence_constraints

        puts ']'

        #print_validators
        #print_has
      end

      private

      def extract_primary_keys # ✓
        puts '// Primary keys.'
        @table_names.each do |table_name|
          primary_key = @conn.primary_key(table_name)
          if not primary_key
            puts "// Error #{table_name} has nil primary key"
            next
          end
          print_constraint("unique", table_name, [primary_key])
        end
        puts
      end

      def extract_unique_constraints # ✓
        puts '// Uniqueness constraints from indexes and validations.'
        extract_unique_indexes
        extract_uniqueness_validations
        puts
      end

      def extract_unique_indexes # ✓
        @table_names.each do |table_name|
          @conn.indexes(table_name).select(&:unique).each do |index|
            print_constraint("unique", table_name, index.columns)
          end
        end
      end

      def extract_uniqueness_validations # ✓
        @models.each do |model|
          process_uniqueness_validators(model)
        end
      end

      # in Diaspora these constraints do not appear in the manually generated constraints
      # This represents 59 unused constraints
      def extract_timestamp_constraints
        puts '// Timestamp NOT NULL constraints'
        @table_names.each do |table_name|
          columns = @conn.columns(table_name)
          print_non_null(table_name, 'created_at') if columns.any? { |c| c.name == 'created_at' }
          print_non_null(table_name, 'updated_at') if columns.any? { |c| c.name == 'updated_at' }
        end
        puts
      end

      def extract_enum_constraints # ✓
        puts '// Enum constraints'
        @models.each do |model|
          table_name = model.table_name
          model.defined_enums.each do |attr, defs|
            next unless defs.is_a?(Hash)

            column = model.columns_hash[attr.to_s]
            next unless column

            values = defs.values
            unless values.all?(Integer)
              puts "// Error: Enum constraint for table #{table_name} with values (#{values}) not all Integers"
              next
            end

            print_one_of_int(table_name, column.name, values.join(', '))
          end
        end
        puts
      end

      def extract_presence_constraints # ✓
        puts '// Presence and numericality constraints'
        @models.each do |model|
          extract_model_presence_constraints(model)
          extract_model_numericality_constraints(model)
          extract_model_length_constraints(model)
          extract_model_inclusion_constraints(model)
        end
        puts
      end

      def extract_model_presence_constraints(model) # ✓
        model.validators.each do |validator|
          next unless validator.is_a?(ActiveRecord::Validations::PresenceValidator)

          validator.attributes.each do |attr|
            column = model.columns_hash[attr.to_s]
            next unless column
            print_non_null(model.table_name, column.name)

            if STRING_LIKE_TYPES.include?(column.type)
              print_string_is_not_empty(model.table_name, column.name)
              #puts "{ type = \"string-is-not-empty\", tbl = \"#{model.table_name}\", col = \"#{column.name}\" },"
            end
          end
          # FIXME(kerneyj) So there is actually a lot more to the above, the real question is how to support spaces in the string encoding for Z3
        end
      end

      def extract_model_length_constraints(model) # ✓
        model.validators.each do |validator|
          next unless validator.is_a?(ActiveModel::Validations::LengthValidator) && validator.options[:minimum]&.positive?

          validator.attributes.each do |attr|
            column = model.columns_hash[attr.to_s]
            next unless column
            print_non_null(table_name, column.name)

            if STRING_LIKE_TYPES.include?(column.type)
              print_string_is_not_empty(table_name, column.name)
              #puts "{ type = \"string-is-not-empty\", tbl = \"#{table_name}\", col = \"#{column.name}\" },"
            end
          end
          # FIXME(kerneyj) So there is actually a lot more to the above, the real question is how to support spaces in the string encoding for Z3
        end
      end

      def extract_model_numericality_constraints(model) # ✓
        model.validators.select { |v| v.is_a?(ActiveModel::Validations::NumericalityValidator) }.each do |num_v|
          options = num_v.options.dup
          allow_nil = options.delete(:allow_nil)
          greater_than_or_equal_to = options.delete(:greater_than_or_equal_to)
          options.delete(:only_integer)
          next unless options.empty?

          num_v.attributes.each do |attr|
            column = model.columns_hash[attr.to_s]
            next unless column

            print_non_null(model.table_name, column.name) unless allow_nil
            if greater_than_or_equal_to
              sql = "SELECT 1 FROM `#{model.table_name}` WHERE `#{column.name}` < #{greater_than_or_equal_to}"
              if model.has_attribute?(model.inheritance_column)
                # TODO(zhangwen): also applies to descendants.
                sql += " AND `#{model.inheritance_column}` = '#{model.sti_name}'"
              end
              print_query_is_empty(sql)
            end
          end
        end
      end

      def extract_model_inclusion_constraints(model)
        model.validators.select { |v| v.is_a?(ActiveModel::Validations::InclusionValidator) }.each do |inc_v|
          options = inc_v.options.dup
          allow_nil = options.delete(:allow_nil)
          allowed_values = options.delete(:in) || options.delete(:within)
          options.delete(:within)
          options.delete(:message)

          next unless options.empty?
          next unless allowed_values.is_a?(Array)
          next unless allowed_values.all? { |v| v.is_a?(String) }

          inc_v.attributes.each do |attr|
            column = model.columns_hash[attr.to_s]
            next unless column

            print_non_null(model.table_name, column.name) unless allow_nil
            print_one_of_string(model.table_name, column.name, allowed_values)
          end
        end
      end

      def extract_foreign_keys # ✓
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

      def handle_standard_association(from_tbl, from_col, association) # ✓
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
          # type = is_optional ? 'foreign-key' : 'foreign-key-non-null'
          # type = 'foreign-key'
          print_foreign_key(to_tbl, to_col, from_tbl, from_col, false)
        end
      end

      def handle_sti_association(from_tbl, from_col, to_tbl, to_col, to_klass, is_optional) # ✓
        to_type = to_klass.sti_name
        print_query_is_set_contained_in(
          "SELECT `#{from_col}` FROM `#{from_tbl}`",
          "SELECT `#{to_col}` FROM `#{to_tbl}` WHERE `#{to_klass.inheritance_column}` = '#{to_type}'"
        )
        print_non_null(from_tbl, from_col) unless is_optional
      end

      def handle_polymorphic_association(from_tbl, from_col, association) # ✓
        return if association.options[:optional] # FIXME(kerneyj) If the has_one/has_many on the otherside is not optional then this is incorrect
        # furthermore, if the column in both models are optional then does should we not include the foreign key constraint

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

      def find_polymorphic_inverses(association) # ✓
        @models.flat_map(&:reflect_on_all_associations).select do |a|
          HAS_MACROS.include?(a.macro) &&
          a.options[:as] == association.name &&
          a.klass == association.active_record
        end
      end

      def handle_single_inverse(from_tbl, from_col, inverse) # ✓
        to_tbl = inverse.active_record.table_name
        to_col = inverse.join_foreign_key
        print_foreign_key(to_tbl, to_col, from_tbl, from_col, true)
      end

      def handle_multiple_inverses(from_tbl, from_col, from_type_col, inverses) # ✓
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

      def process_uniqueness_validators(model) # ✓
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

      def print_constraint(type, table, columns) # ✓
        column_list = columns.map { |c| "\"#{c}\"" }.join(', ')
        puts "{ type = \"#{type}\", tbl = \"#{table}\", cols = [#{column_list}] },"
      end

      def print_foreign_key(totbl, tocol, frtbl, frcol, nonnull)
        if nonnull
          puts "{ type = foreign-key-non-null, from-tbl = \"#{frtbl}\", from-col = \"#{frcol}\", to-tbl = \"#{totbl}\", to-col = \"#{tocol}\" },"
        else
          puts "{ type = foreign-key, from-tbl = \"#{frtbl}\", from-col = \"#{frcol}\", to-tbl = \"#{totbl}\", to-col = \"#{tocol}\" },"
        end
      end

      def print_one_of_int(tbl, col, values)
        puts "{ type = \"one-of-int\", tbl = \"#{tbl}\", col = \"#{col}\", allowed-values = [#{values}] },"
      end

      def print_query_is_empty(sql)
        puts "{ type = \"query-is-empty\", sql = \"#{sql}\" },"
      end

      def print_string_is_not_empty(tbl, col)
        puts "{ type = \"string-is-not-empty\", tbl = \"#{tbl}\", col = \"#{col}\" },"
      end

      def print_non_null(tbl, col) # ✓
        puts "{ type = \"non-null\", tbl = \"#{tbl}\", col = \"#{col}\" },"
      end

      def print_one_of_string(tbl, col, allowed_values) # ✓
        allowed_values = allowed_values.uniq
        values_list = allowed_values.map { |v| "\"#{v}\"" }.join(', ')
        puts "{ type = \"one-of-string\", tbl = \"#{tbl}\", col = \"#{col}\", allowed-values = [#{values_list}] },"
      end

      def print_query_is_set_contained_in(sql1, sql2) # ✓
        puts "{ type = \"query-is-set-contained-in\", sql-1 = \"#{sql1}\", sql-2 = \"#{sql2}\" },"
      end
    end

    def print_validators
      @models.each do |model|
        model.validators.each do |validator|
          table_name = model.table_name
          next unless validator.is_a?(ActiveRecord::Validations::AssociatedValidator)
          validator.attributes.each do |attr|
            puts "#{table_name}: #{attr.to_s}"
          end
        end
      end
    end

    def print_has
      @models.each do |model|
        next unless model.table_name == "posts"
        puts "table name: #{model.table_name}"
        model.reflect_on_all_associations(:has_one).each do |association|
          pp association
        end

        model.reflect_on_all_associations(:has_many).each do |association|
          pp association
        end
        puts
      end
    end
    ConstraintExtractor.new.extract_all
  end
end
