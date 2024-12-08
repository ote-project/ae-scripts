# To use this rake task: In the `lib/tasks` directory of your Rails application, create a symbolic link to this file;
# then run `bin/rake constraints:extract` in the application directory.  The constraints will be printed to stdout.
namespace :constraints do
  desc 'Extracts database constraints for an application'

  task extract: :environment do
    string_like_types = %i[string text]
    has_macros = %i[has_one has_many]

    puts 'constraints = ['

    conn = ActiveRecord::Base.connection

    Rails.application.eager_load!
    models = ActiveRecord::Base.descendants.select { |m| m.table_name.present? }
    table_names = models.map(&:table_name).uniq

    puts '// Primary keys.'
    table_names.each do |table_name|
      primary_key = conn.primary_key(table_name)
      next unless primary_key
      puts "{ type = \"unique\", tbl = \"#{table_name}\", cols = [\"#{primary_key}\"] },"
    end
    puts

    puts '// Uniqueness.'
    table_names.each do |table_name|
      indexes = conn.indexes(table_name)
      indexes.select(&:unique).each do |index|
        columns = index.columns
        puts "{ type = \"unique\", tbl = \"#{table_name}\", cols = [#{columns.map { |c| "\"#{c}\"" }.join(', ')}] },"
      end
    end

    models.each do |model|
      table_name = model.table_name
      model.validators.each do |validator|
        next unless validator.is_a?(ActiveRecord::Validations::UniquenessValidator)

        options = validator.options.dup
        options.delete(:allow_blank)
        allow_nil = options.delete(:allow_nil)
        scope = Array(options.delete(:scope))
        next unless options.delete(:case_sensitive)
        next unless options.empty? # We don't support other options.

        attributes = validator.attributes
        next unless attributes.size == 1 # There should be one and only one attribute. TODO: Validate this.

        attribute = attributes.first
        column = model.columns_hash[attribute.to_s]
        next unless column

        uniq_col_names = [column.name] + scope.map(&:to_s)
        puts "{ type = \"unique\", tbl = \"#{table_name}\", cols = [#{uniq_col_names.map { |c| "\"#{c}\"" }.join(', ')}] },"

        # Validate uniqueness without allow_nil
        print_non_null(table_name, column.name) unless allow_nil
      end
    end

    puts

    puts '// Foreign keys.'
    models.each do |model|
      from_tbl = model.table_name
      model.reflect_on_all_associations(:belongs_to).each do |association|
        from_col = association.foreign_key
        if association.polymorphic?
          next if association.options[:optional]

          from_type_col = association.foreign_type
          print_non_null(from_tbl, from_type_col)

          inverses = models.flat_map(&:reflect_on_all_associations).select do |a|
            has_macros.include?(a.macro) && a.options[:as] == association.name && a.klass == model
          end
          next if inverses.empty? # TODO(zhangwen): what's the deal with ActiveAdmin Comment?

          all_type_names = inverses.map { |a| a.active_record.name }
          print_one_of_string(from_tbl, from_type_col, all_type_names)

          if inverses.length == 1
            a = inverses.first
            to_tbl = a.active_record.table_name
            to_col = a.join_foreign_key
            puts "{ type = \"foreign-key-non-null\", from-tbl = \"#{from_tbl}\", from-col = \"#{from_col}\", to-tbl = \"#{to_tbl}\", to-col = \"#{to_col}\" },"
          else
            inverses.each do |a|
              type_name = a.active_record.name
              to_tbl = a.active_record.table_name
              to_col = a.join_foreign_key
              print_query_is_set_contained_in(
                "SELECT `#{from_col}` FROM `#{from_tbl}` WHERE `#{from_type_col}` = '#{type_name}'",
                "SELECT `#{to_col}` FROM `#{to_tbl}`"
              )
            end
          end
        else
          begin
            to_klass = association.klass
            to_tbl = to_klass.table_name
          rescue NameError => e
            puts "// Error: #{e}"
            next
          end
          to_col = association.association_primary_key
          is_optional = association.options[:optional]

          if to_klass.has_attribute?(to_klass.inheritance_column) && to_klass.descendants.empty?
            # Using single-table inheritance, and has no subclasses.
            # So the "to-type" must be the STI name of the class.
            to_type = to_klass.sti_name
            print_query_is_set_contained_in(
              "SELECT `#{from_col}` FROM `#{from_tbl}`",
              "SELECT `#{to_col}` FROM `#{to_tbl}` WHERE `#{to_klass.inheritance_column}` = '#{to_type}'"
            )
            print_non_null(from_tbl, from_col) unless is_optional
          else
            type = is_optional ? 'foreign-key' : 'foreign-key-non-null'
            puts "{ type = \"#{type}\", from-tbl = \"#{from_tbl}\", from-col = \"#{from_col}\", to-tbl = \"#{to_tbl}\", to-col = \"#{to_col}\" },"
          end
        end
      end
    end
    puts

    puts '// Constrain `created_at` and `updated_at` to be NOT NULL.'
    table_names.each do |table_name|
      columns = conn.columns(table_name)
      print_non_null(table_name, 'created_at') if columns.any? { |c| c.name == 'created_at' }
      print_non_null(table_name, 'updated_at') if columns.any? { |c| c.name == 'updated_at' }
    end
    puts

    puts '// Enums.'
    models.each do |model|
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

    puts '// Presence and numericality.'
    models.each do |model|
      table_name = model.table_name
      model.validators.each do |validator|
        next unless validator.is_a?(ActiveModel::Validations::PresenceValidator) ||
                    (validator.is_a?(ActiveModel::Validations::LengthValidator) && validator.options[:minimum]&.positive?)

        validator.attributes.each do |attr|
          column = model.columns_hash[attr.to_s]
          next unless column

          # TODO(zhangwen): Not needed if the column is already constrained to be NOT NULL.
          print_non_null(table_name, column.name)

          # If the attribute is string or text, also constrain it to be not empty.
          if string_like_types.include?(column.type)
            puts "{ type = \"string-is-not-empty\", tbl = \"#{table_name}\", col = \"#{column.name}\" },"
          end
        end
      end

      model.validators.select { |v| v.is_a?(ActiveModel::Validations::NumericalityValidator) }.each do |num_v|
        options = num_v.options.dup
        allow_nil = options.delete(:allow_nil)
        greater_than_or_equal_to = options.delete(:greater_than_or_equal_to)
        options.delete(:only_integer)
        next unless options.empty?

        num_v.attributes.each do |attr|
          column = model.columns_hash[attr.to_s]
          next unless column

          puts "{ type = \"non-null\", tbl = \"#{table_name}\", col = \"#{column.name}\" }," unless allow_nil
          if greater_than_or_equal_to
            sql = "SELECT 1 FROM `#{table_name}` WHERE `#{column.name}` < #{greater_than_or_equal_to}"
            if model.has_attribute?(model.inheritance_column)
              # TODO(zhangwen): also applies to descendants.
              sql += " AND `#{model.inheritance_column}` = '#{model.sti_name}'"
            end
            puts "{ type = \"query-is-empty\", sql = \"#{sql}\" },"
          end
        end
      end
    end
    puts

    puts '// Inclusion.'
    models.each do |model|
      table_name = model.table_name
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

          puts "{ type = \"non-null\", tbl = \"#{table_name}\", col = \"#{column.name}\" }," unless allow_nil
          print_one_of_string(table_name, column.name, allowed_values)
        end
      end
    end

    puts '// Restrict the "type" field of base classes.'
    models.each do |model|
      table_name = model.table_name
      inheritance_column = model.inheritance_column
      next unless model.has_attribute?(inheritance_column)
      next unless model.base_class == model

      all_type_names = (model.descendants + [model]).map(&:sti_name)
      print_one_of_string(table_name, inheritance_column, all_type_names)
    end
    puts

    puts ']'
  end

  def print_non_null(tbl, col)
    puts "{ type = \"non-null\", tbl = \"#{tbl}\", col = \"#{col}\" },"
  end

  def print_one_of_string(tbl, col, allowed_values)
    allowed_values = allowed_values.uniq
    puts "{ type = \"one-of-string\", tbl = \"#{tbl}\", col = \"#{col}\", allowed-values = [#{allowed_values.map { |v| "\"#{v}\"" }.join(', ')}] },"
  end

  def print_query_is_set_contained_in(sql1, sql2)
    puts "{ type = \"query-is-set-contained-in\", sql-1 = \"#{sql1}\", sql-2 = \"#{sql2}\" },"
  end
end
